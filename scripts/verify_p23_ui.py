"""P23 UI check: send a pilot trace to Langfuse and verify it is readable via API.

Run: python scripts/verify_p23_ui.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import httpx

from agent_obs import ObservabilitySDK, PriceBook, Usage
from agent_obs.exporters.langfuse_exporter import LangfuseExporter

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-your-public-key-here")
SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-your-secret-key-here")


async def send_trace() -> str:
    """Create and export one trace; return its trace_id."""
    exporter = LangfuseExporter(
        endpoint=LANGFUSE_HOST,
        public_key=PUBLIC_KEY,
        secret_key=SECRET_KEY,
    )
    sdk = ObservabilitySDK(exporters=[exporter], enabled=True)
    price_book = PriceBook.load("price_book.yaml")

    @sdk.agent_observed("pilot-agent", version="1.0.0")
    async def run(_obs_ctx=None) -> str:
        async with sdk.llm_call(
            _obs_ctx,
            model="gpt-4o",
            provider="openai",
            price_book=price_book,
            usage=Usage(input=120, output=45, cached=10),
            input_text="P23 verification query",
            output_text="P23 verification answer",
        ) as llm_span:
            llm_span.add_event("llm.call.started")
            llm_span.add_event("llm.call.completed")
        async with sdk.tool_call(_obs_ctx, tool_name="search") as tool_span:
            tool_span.set_attribute("status", "ok")
        return "done"

    await run()
    await sdk.shutdown()

    # Langfuse stores the trace id in the hex form we send over OTLP,
    # not the SDK's Crockford base32 form.
    from agent_obs.exporters.langfuse_exporter import _to_hex

    trace_id = _to_hex(sdk.last_spans[-1].context.trace_id).zfill(32)
    return trace_id


async def verify(trace_id: str) -> bool:
    """Check Langfuse API returns a readable trace for *trace_id*."""
    auth = base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        # Poll briefly: export is async (fire-and-forget).
        trace = None
        for _ in range(20):
            r = await client.get(f"{LANGFUSE_HOST}/api/public/traces/{trace_id}")
            if r.status_code == 200:
                trace = r.json()
                break
            await asyncio.sleep(0.5)
        if trace is None:
            print(f"FAIL: trace {trace_id} not found (last status {r.status_code})")
            return False

        r_obs = await client.get(
            f"{LANGFUSE_HOST}/api/public/observations",
            params={"traceId": trace_id},
        )
        observations = r_obs.json().get("data", []) if r_obs.status_code == 200 else []

        ok = True

        # 1. Tree: root + children present.
        # v3 names tool observations by their gen_ai.tool.name ("search"),
        # so accept both the prefixed and the plain tool name.
        names = {o.get("name", "") for o in observations}
        root = [n for n in names if n.startswith("agent.loop:")]
        llm = [n for n in names if n.startswith("llm.call:")]
        tool = [n for n in names if n.startswith("tool.call:") or n == "search"]
        if not root or not llm or not tool:
            print(f"FAIL: tree incomplete, observations={sorted(names)}")
            ok = False
        else:
            print(f"OK: tree = {sorted(names)}")

        # 2. Tokens: native usage on the llm observation (GENERATION).
        llm_obs = next((o for o in observations if o.get("name", "").startswith("llm.call:")), None)
        if llm_obs:
            usage = llm_obs.get("usage") or {}
            input_t = usage.get("input") or llm_obs.get("usageDetails", {}).get("input")
            output_t = usage.get("output") or llm_obs.get("usageDetails", {}).get("output")
            meta = llm_obs.get("metadata") or {}
            attrs = meta.get("attributes") or {}
            tok_in = attrs.get("tokens.input") or attrs.get("gen_ai.usage.input_tokens")
            if input_t and output_t:
                print(f"OK: native usage input={input_t} output={output_t}")
            elif tok_in is not None:
                print(f"OK: metadata tokens input={tok_in} (native usage empty)")
            else:
                print(f"FAIL: no tokens visible; usage={usage} metadata_keys={list(meta)}")
                ok = False

            # 3. Cost visible: native costDetails or the cost.usd attribute.
            cost = (llm_obs.get("costDetails") or {}).get("total") or attrs.get("cost.usd")
            if cost is None:
                print(f"FAIL: cost not visible; costDetails={llm_obs.get('costDetails')} attrs.keys={list(attrs)}")
                ok = False
            else:
                print(f"OK: cost = {cost}")

        # 4. Status visible on root.
        root_obs = next((o for o in observations if o.get("name", "").startswith("agent.loop:")), None)
        if root_obs:
            status = root_obs.get("status") or root_obs.get("level")
            root_attrs = (root_obs.get("metadata") or {}).get("attributes") or {}
            agg_cost = root_attrs.get("cost.usd_sum")
            steps = root_attrs.get("steps.count")
            if status:
                print(f"OK: root status = {status}")
            else:
                print(f"FAIL: root status missing; level/status={status!r}")
                ok = False
            print(f"OK: root aggregate cost.usd_sum={agg_cost} steps.count={steps}")

        # 5. Events visible: mirror attribute on the llm observation.
        if llm_obs:
            attrs = (llm_obs.get("metadata") or {}).get("attributes") or {}
            raw_events = attrs.get("agent_obs.events") or []
            if isinstance(raw_events, str):
                try:
                    raw_events = json.loads(raw_events)
                except (ValueError, TypeError):
                    raw_events = []
            event_names = set(raw_events or [])
            if {"llm.call.started", "llm.call.completed"} <= event_names:
                print(f"OK: events = {sorted(event_names)}")
            else:
                print(f"FAIL: events missing, got {sorted(event_names)}")
                ok = False

        return ok


async def main() -> int:
    trace_id = await send_trace()
    print(f"sent trace_id={trace_id}")
    ok = await verify(trace_id)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
