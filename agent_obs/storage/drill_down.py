"""
DrillDown API: восстановление конкретных полей trace из tiered storage.

PP21: DrillDownService реализует API для восстановления полей трейсов из hot/warm/cold
с RBAC + MFA для PII-полей, Redis-кешем, rate-limit и метриками.
"""

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import pyarrow as pa
import redis.asyncio as redis
from agent_obs.storage.cold import ColdStore
from agent_obs.storage.hot import HotStore
from agent_obs.storage.warm import WarmStore


@dataclass
class DrillDownRequest:
    """Запрос на drill-down для восстановления полей трейса."""
    trace_id: str
    fields: List[str] = field(default_factory=list)  # пустой = все доступные поля tier'а
    user: str = ""
    mfa_used: bool = False  # endpoint верифицировал MFA до вызова


@dataclass
class DrillDownResult:
    """Результат drill-down запроса."""
    fields: Dict[str, Any]
    storage_tier: str  # "hot" | "warm" | "cold"
    query_latency_ms: float
    mfa_used: bool
    cache_hit: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class DrillDownService:
    """Сервис восстановления полей трейсов из tiered storage с RBAC и кешем."""
    
    PII_FIELDS = {
        "llm.input_text",
        "llm.output_text", 
        "tool.input",
        "tool.output",
        "input_text",
        "output_text",
        "input",
        "output",
    }
    
    def __init__(
        self,
        hot_store: HotStore,
        warm_store: WarmStore,
        cold_store: ColdStore,
        redis_client: Optional[redis.Redis] = None,
        rate_limit: Optional[int] = None,
        cache_ttl: int = 600,
        athena_query: Optional[callable] = None,
    ):
        self.hot_store = hot_store
        self.warm_store = warm_store
        self.cold_store = cold_store
        self.redis_client = redis_client
        self.rate_limit = int(os.environ.get("DRILL_DOWN_RATE_LIMIT", 10))
        self.cache_ttl = cache_ttl
        self.athena_query = athena_query
        
    async def _rate_limit_check(self, user: str) -> bool:
        """Проверка rate-limit: max self.rate_limit запросов в минуту на пользователя.
        
        Returns True если запрос разрешен, False если превышен лимит.
        Fail-open при ошибках Redis (вернуть True, чтобы не блокировать бизнес-логику).
        """
        if not self.redis_client:
            return True
        
        window = int(time.time() // 60)
        key = f"agent_obs:drill_down:rate:{user}:{window}"
        
        try:
            result = await asyncio.to_thread(
                self.redis_client.incr,
                key
            )
            
            if result == 1:  # Первый запрос в окне — установить TTL
                await asyncio.to_thread(
                    self.redis_client.expire,
                    key,
                    60
                )
            
            # Ensure result is an integer
            if isinstance(result, int):
                return result <= self.rate_limit
            else:
                # If Redis returns something unexpected, allow the request
                return True
            
        except Exception as e:
            # Логируем ошибку, но fail-open (не блокируем запрос)
            from agent_obs.metrics import drill_down_rate_limited_total
            try:
                drill_down_rate_limited_total.labels(
                    tier="unknown",
                    outcome="redis_error"
                ).inc()
            except Exception:
                pass  # Ignore metric errors too
            return True
    
    async def _cache_get(self, cache_key: str) -> Optional[DrillDownResult]:
        """Получить результат из Redis-кеша."""
        if not self.redis_client:
            return None
        
        try:
            cached_data = await asyncio.to_thread(
                self.redis_client.get,
                cache_key
            )
            if cached_data:
                data = json.loads(cached_data)
                return DrillDownResult(**data)
        except Exception:
            pass
        return None
    
    async def _cache_set(self, cache_key: str, result: DrillDownResult) -> None:
        """Сохранить результат в Redis-кеш."""
        if not self.redis_client:
            return
        
        try:
            data = {
                "fields": result.fields,
                "storage_tier": result.storage_tier,
                "query_latency_ms": result.query_latency_ms,
                "mfa_used": result.mfa_used,
                "cache_hit": result.cache_hit,
                "metadata": result.metadata
            }
            await asyncio.to_thread(
                self.redis_client.setex,
                cache_key,
                self.cache_ttl,
                json.dumps(data)
            )
        except Exception:
            pass
    
    async def _hot_tier_recover(self, trace_id: str, fields: List[str]) -> Dict[str, Any]:
        """Восстановить поля из hot tier (ClickHouse spans_hot)."""
        spans = await asyncio.to_thread(self.hot_store.get_spans, trace_id)
        if not spans:
            raise ValueError("Trace not found in hot tier")
        
        recovered = {}
        if not fields:
            # Вернуть все атрибуты из всех спанов
            for span in spans:
                if "attributes" in span:
                    recovered.update(span["attributes"])
        else:
            # Вернуть запрошенные поля
            for field_name in fields:
                for span in spans:
                    if "attributes" in span and field_name in span["attributes"]:
                        recovered[field_name] = span["attributes"][field_name]
                        break
        
        return recovered
    
    async def _warm_tier_recover(self, trace_id: str, fields: List[str]) -> Dict[str, Any]:
        """Восстановить поля из warm tier (Postgres traces_warm)."""
        trace = await self.warm_store.get_trace(trace_id)
        if not trace:
            raise ValueError("Trace not found in warm tier")
        
        # Warm tier содержит только метаданные, не сырые тексты
        recovered = {}
        warm_columns = {
            "trace_id", "tenant_id", "agent_id", "start_time", "end_time",
            "status", "cost_usd_total", "span_count", "error_count",
            "eval_avg", "input_chars", "input_sha256", "output_chars",
            "output_sha256", "user_hash"
        }
        
        if not fields:
            # Вернуть все доступные колонки
            for col in warm_columns:
                if col in trace:
                    recovered[col] = trace[col]
        else:
            # Вернуть запрошенные поля, которые существуют в warm tier
            for field_name in fields:
                if field_name in warm_columns and field_name in trace:
                    recovered[field_name] = trace[field_name]
        
        return recovered
    
    async def _cold_tier_recover(self, trace_id: str, fields: List[str], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Восстановить поля из cold tier (S3 Parquet), best-effort.
        
        Если в Parquet есть колонка trace_id — фильтровать по ней.
        Иначе вернуть агрегаты партицы + metadata.cold_note.
        """
        metadata = {}
        
        # Проверяем Athena-хук (для PP22)
        if self.athena_query:
            try:
                # PP22: делегировать Athena-запрос
                return await self.athena_query(trace_id, fields)
            except Exception as e:
                metadata["cold_note"] = f"Athena query failed: {str(e)}"
        
        # PP21: прямой Parquet-чтение
        try:
            # Best-effort: ищем файлы с tenant_id если известен
            prefix = ""
            if tenant_id:
                # Предположим структуру tenant_id/year/month/day
                # Это упрощение — реальная логика должна быть согласована с maintenance.py
                import datetime
                today = datetime.datetime.now(datetime.timezone.utc)
                prefix = f"tenant_id={tenant_id}/year={today.year}/month={today.month}/day={today.day}"
            
            keys = await self.cold_store.list_keys(prefix)
            
            for key in keys:
                try:
                    parquet_bytes = await asyncio.to_thread(self.cold_store.read_bytes, key)
                    table = pa.parquet.read_table(pa.pyarrow.BufferReader(parquet_bytes))
                    
                    # Проверяем наличие колонки trace_id
                    if "trace_id" in table.schema.names:
                        # Фильтруем по trace_id
                        mask = table["trace_id"] == trace_id
                        filtered_table = table.filter(mask)
                        
                        if len(filtered_table) > 0:
                            # Возвращаем запрошенные поля из первой найденной строки
                            row = filtered_table.to_pylist()[0]
                            recovered = {}
                            if not fields:
                                recovered = dict(row)
                            else:
                                for field_name in fields:
                                    if field_name in row:
                                        recovered[field_name] = row[field_name]
                            return {"fields": recovered, "metadata": metadata}
                    
                except Exception as e:
                    continue
            
            # Если trace_id не найден, возвращаем агрегаты партицы
            metadata["cold_note"] = "Trace not found in cold Parquet files; returning partition aggregates"
            
            # Если есть данные в таблице, возвращаем агрегаты
            if len(table) > 0:
                row = table.to_pylist()[0]
                recovered = {}
                if not fields:
                    recovered = dict(row)
                else:
                    for field_name in fields:
                        if field_name in row:
                            recovered[field_name] = row[field_name]
                return {"fields": recovered, "metadata": metadata}
            
        except Exception as e:
            metadata["cold_note"] = f"Parquet read failed: {str(e)}"
        
        raise ValueError("Trace not found in cold tier")
    
    async def drill_down(self, request: DrillDownRequest) -> DrillDownResult:
        """Выполнить drill-down запрос для восстановления полей трейса.
        
        Flow:
        1. Rate-limit проверка
        2. Defense-in-depth guard для PII полей
        3. Кеш проверка
        4. Tier resolution (hot -> warm -> cold)
        5. Кеш сохранение
        6. Метрики
        """
        start_time = time.time()
        
        # 1. Rate-limit проверка
        if not await self._rate_limit_check(request.user):
            from agent_obs.metrics import drill_down_rate_limited_total
            drill_down_rate_limited_total.labels(
                tier="unknown",
                outcome="rate_limited"
            ).inc()
            raise ValueError("Rate limit exceeded")
        
        # 2. Defense-in-depth guard для PII полей
        if any(field in self.PII_FIELDS for field in request.fields) and not request.mfa_used:
            raise PermissionError("MFA required for PII fields")
        
        # 3. Кеш проверка
        cache_key = hashlib.sha256(
            f"{request.trace_id}:{','.join(sorted(request.fields))}:{request.user}".encode()
        ).hexdigest()
        
        cached_result = await self._cache_get(cache_key)
        if cached_result:
            cached_result.cache_hit = True
            cached_result.query_latency_ms = (time.time() - start_time) * 1000
            return cached_result
        
        # 4. Tier resolution
        tier = None
        fields = {}
        metadata = {}
        
        # Hot tier
        try:
            fields = await self._hot_tier_recover(request.trace_id, request.fields)
            tier = "hot"
        except Exception:
            # Warm tier
            try:
                fields = await self._warm_tier_recover(request.trace_id, request.fields)
                tier = "warm"
            except Exception:
                # Cold tier
                try:
                    # tenant_id пока неизвестен в запросе — best-effort без tenant
                    cold_result = await self._cold_tier_recover(request.trace_id, request.fields)
                    fields = cold_result["fields"]
                    metadata.update(cold_result["metadata"])
                    tier = "cold"
                except Exception:
                    raise ValueError("Trace not found in any tier")
        
        latency_ms = (time.time() - start_time) * 1000
        
        # 5. Кеш сохранение
        result = DrillDownResult(
            fields=fields,
            storage_tier=tier,
            query_latency_ms=latency_ms,
            mfa_used=request.mfa_used,
            metadata=metadata
        )
        await self._cache_set(cache_key, result)
        
        # 6. Метрики
        from agent_obs.metrics import drill_down_latency_seconds
        drill_down_latency_seconds.labels(tier=tier).observe(latency_ms)
        
        return result