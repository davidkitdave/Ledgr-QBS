"""Register ledgr light custom metrics with ADK (pytest + agents-cli parity)."""

from __future__ import annotations

from google.adk.evaluation.custom_metric_evaluator import _CustomMetricEvaluator
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_metrics import Interval, MetricInfo, MetricValueInfo
from google.adk.evaluation.metric_evaluator_registry import DEFAULT_METRIC_EVALUATOR_REGISTRY


def _metric_info(metric_name: str, description: str = "") -> MetricInfo:
    return MetricInfo(
        metric_name=metric_name,
        description=description,
        metric_value_info=MetricValueInfo(interval=Interval(min_value=0.0, max_value=1.0)),
    )


def register_ledgr_light_custom_metrics(eval_config: EvalConfig) -> None:
    """Mirror agents-cli ``eval grade`` custom-metric registration."""
    if not eval_config.custom_metrics:
        return
    for metric_name, config in eval_config.custom_metrics.items():
        if config.metric_info:
            metric_info = config.metric_info.model_copy()
            metric_info.metric_name = metric_name
        else:
            metric_info = _metric_info(metric_name, config.description or "")
        DEFAULT_METRIC_EVALUATOR_REGISTRY.register_evaluator(
            metric_info, _CustomMetricEvaluator
        )
