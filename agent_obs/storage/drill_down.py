"""
DrillDown API: восстановление конкретных полей trace из tiered storage.

PP21: DrillDownService реализует API для восстановления полей трейсов из hot/warm/cold
с RBAC + MFA для PII-полей, Redis-кешем, rate-limit и метриками.
PP23: Redis-кеш drill-down результатов (TTL 600с) с инвалидацией по
late annotation / vault recovery, метриками hit-ratio и cache-size.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import pyarrow as pa
import redis.asyncio as redis
from agent_obs.storage.cold import ColdStore
from agent_obs.storage.hot import HotStore
from agent_obs.storage.warm import WarmStore

logger = logging.getLogger(__name__)

# PP23: Redis key prefixes for drill-down cache and its invalidation indexes.
CACHE_KEY_PREFIX = "agent_obs:drill_down:cache:"
IDX_TRACE_PREFIX = "agent_obs:drill_down:idx:trace:"
IDX_USER_TRACE_PREFIX = "agent_obs:drill_down:idx:user_trace:"
CACHE_BYTES_KEY = "agent_obs:drill_down:cache:bytes"


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


async def _invalidate_by_index(
    redis_client: Optional[redis.Redis], index_key: str
) -> int:
    """Удалить все кеш-ключи, входящие в индекс-сет index_key (PP23).
    
    Best-effort: при ошибке Redis логирует предупреждение и возвращает 0,
    чтобы инвалидация не блокировала бизнес-логику.
    
    Returns:
        Количество удалённых кеш-ключей.
    """
    if not redis_client:
        return 0
    
    try:
        cache_keys = await asyncio.to_thread(redis_client.smembers, index_key)
        deleted = 0
        if cache_keys:
            # Преобразуем байты → str если нужно (redis-py с decode_responses=False)
            normalized = [
                k.decode() if isinstance(k, bytes) else k for k in cache_keys
            ]
            deleted = await asyncio.to_thread(redis_client.delete, *normalized)
        # Удаляем и сам индекс
        await asyncio.to_thread(redis_client.delete, index_key)
        return int(deleted)
    except Exception as e:
        logger.warning(
            "drill-down cache invalidation failed for index=%s: %s", index_key, e
        )
        return 0


async def invalidate_drill_down_cache(
    redis_client: Optional[redis.Redis], trace_id: str, user: Optional[str] = None
) -> int:
    """Инвалидировать drill-down кеш для trace_id (+опционально user) (PP23).
    
    Module-level helper для вызова извне (late_annotation, vault_client)
    без создания DrillDownService.
    
    Args:
        redis_client: sync-подобный redis.Redis клиент (используется через to_thread).
        trace_id: Trace для инвалидации (все user'ы).
        user: если указан — инвалидируются только ключи этого user'а.
    
    Returns:
        Количество удалённых кеш-ключей.
    """
    if not redis_client:
        return 0
    
    if user:
        return await _invalidate_by_index(
            redis_client, f"{IDX_USER_TRACE_PREFIX}{user}:{trace_id}"
        )
    return await _invalidate_by_index(
        redis_client, f"{IDX_TRACE_PREFIX}{trace_id}"
    )


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
        cache_ttl: Optional[int] = None,
        athena_query: Optional[callable] = None,
    ):
        self.hot_store = hot_store
        self.warm_store = warm_store
        self.cold_store = cold_store
        self.redis_client = redis_client
        # PP23: параметр имеет приоритет над env; env — над дефолтом 10.
        if rate_limit is None:
            rate_limit = int(os.environ.get("DRILL_DOWN_RATE_LIMIT", 10))
        self.rate_limit = int(rate_limit)
        # PP23: параметр имеет приоритет над env; env — над дефолтом 600 (10 мин).
        if cache_ttl is None:
            cache_ttl = int(os.environ.get("DRILL_DOWN_CACHE_TTL", 600))
        self.cache_ttl = int(cache_ttl)
        self.athena_query = athena_query
        # PP23: локальные счётчики для gauge hit-ratio (процесс-локальные).
        self._cache_hits = 0
        self._cache_misses = 0
        
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
    
    async def _cache_set(
        self, cache_key: str, result: DrillDownResult, trace_id: str, user: str
    ) -> None:
        """Сохранить результат в Redis-кеш + проиндексировать для инвалидации (PP23).
        
        Помимо записи значения, добавляет cache_key в Redis-set'ы индексов:
        - idx:trace:{trace_id} — для инвалидации по late annotation
        - idx:user_trace:{user}:{trace_id} — для инвалидации при vault recovery
        Оба индекса и значение пишутся с TTL = self.cache_ttl, чтобы индексы
        не росли бесконечно (устёстчивают сами вместе с кешем).
        
        Также инкрементирует Redis-счётчик CACHE_BYTES_KEY на размер сериализованного
        значения — источник для gauge drill_down_cache_size_bytes.
        """
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
            payload = json.dumps(data)
            
            await asyncio.to_thread(
                self.redis_client.setex,
                cache_key,
                self.cache_ttl,
                payload
            )
            
            # PP23: индекс для инвалидации по trace_id и (user, trace_id).
            trace_idx_key = f"{IDX_TRACE_PREFIX}{trace_id}"
            await asyncio.to_thread(
                self.redis_client.sadd, trace_idx_key, cache_key
            )
            await asyncio.to_thread(
                self.redis_client.expire, trace_idx_key, self.cache_ttl
            )
            
            if user:
                user_idx_key = f"{IDX_USER_TRACE_PREFIX}{user}:{trace_id}"
                await asyncio.to_thread(
                    self.redis_client.sadd, user_idx_key, cache_key
                )
                await asyncio.to_thread(
                    self.redis_client.expire, user_idx_key, self.cache_ttl
                )
            
            # PP23: байт-счётчик для gauge cache size (best-effort, приближённо).
            try:
                await asyncio.to_thread(
                    self.redis_client.incrby,
                    CACHE_BYTES_KEY,
                    len(payload)
                )
            except Exception:
                pass
            
        except Exception:
            pass
    
    async def _update_cache_size_gauge(self) -> None:
        """Обновить gauge drill_down_cache_size_bytes из Redis-счётчика (best-effort)."""
        if not self.redis_client:
            return
        try:
            raw = await asyncio.to_thread(self.redis_client.get, CACHE_BYTES_KEY)
            if raw is not None:
                from agent_obs.metrics import drill_down_cache_size_bytes
                drill_down_cache_size_bytes.set(float(raw))
        except Exception:
            pass
    
    def _update_hit_ratio_gauge(self) -> None:
        """Обновить gauge drill_down_cache_hit_ratio (best-effort)."""
        try:
            from agent_obs.metrics import drill_down_cache_hit_ratio
            total = self._cache_hits + self._cache_misses
            if total > 0:
                drill_down_cache_hit_ratio.set(self._cache_hits / total)
        except Exception:
            pass
    
    async def invalidate_trace(self, trace_id: str) -> int:
        """Инвалидировать все drill-down кеш-ключи для trace_id (PP23).
        
        Вызывается при поступлении late annotation (eval result / summary):
        изменившийся trace не должен отдавать устаревший кеш.
        
        Returns:
            Количество удалённых кеш-ключей.
        """
        return await _invalidate_by_index(
            self.redis_client, f"{IDX_TRACE_PREFIX}{trace_id}"
        )
    
    async def invalidate_user_trace(self, user: str, trace_id: str) -> int:
        """Инвалидировать кеш-ключи для (user, trace_id) (PP23).
        
        Вызывается при vault recovery (user recovered PII): 
        восстановленные данные могут отличаться от закешированных.
        
        Returns:
            Количество удалённых кеш-ключей.
        """
        return await _invalidate_by_index(
            self.redis_client,
            f"{IDX_USER_TRACE_PREFIX}{user}:{trace_id}",
        )
    
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
        
        # 3. Кеш проверка (PP23: ключ включает mfa_used — PII-результаты
        # с MFA и без неё не должны разделять один ключ).
        cache_key = CACHE_KEY_PREFIX + hashlib.sha256(
            f"{request.user}:{request.trace_id}:"
            f"{','.join(sorted(request.fields))}:{request.mfa_used}".encode()
        ).hexdigest()
        
        cached_result = await self._cache_get(cache_key)
        if cached_result:
            # PP23: cache hit — обновляем счётчики и возвращаем закешированное.
            self._cache_hits += 1
            self._update_hit_ratio_gauge()
            cached_result.cache_hit = True
            cached_result.query_latency_ms = (time.time() - start_time) * 1000
            return cached_result
        
        # PP23: cache miss — идём в tier resolution.
        self._cache_misses += 1
        self._update_hit_ratio_gauge()
        
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
        
        # 5. Кеш сохранение (PP23: передаём trace_id и user для индексации).
        result = DrillDownResult(
            fields=fields,
            storage_tier=tier,
            query_latency_ms=latency_ms,
            mfa_used=request.mfa_used,
            metadata=metadata
        )
        await self._cache_set(cache_key, result, request.trace_id, request.user)
        
        # PP23: обновить gauge размера кеша (best-effort).
        await self._update_cache_size_gauge()
        
        # 6. Метрики
        from agent_obs.metrics import drill_down_latency_seconds
        drill_down_latency_seconds.labels(tier=tier).observe(latency_ms)
        
        return result