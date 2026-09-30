"""PC30: adaptive tail-sampling policy engine and OTLP sampler proxy.

The OTel Collector reads ``${env:TAIL_SAMPLER_NORMAL_RATE}`` once at process
start and has no admin API for reloading it, so a dynamic normal-trace rate
cannot live there.  This package provides the two halves of the alternative:

* :mod:`scripts.sampler.policy_engine` — evaluates the load/error rules and
  publishes the resulting rate.
* :mod:`scripts.sampler.sampler_proxy` — an OTLP/HTTP receiver that applies the
  current rate and forwards kept traces downstream.
"""
