"""mystery.apps.reports — AnalysisResult 导出（Excel/HTML）。

只消费 ``[AnalysisResult.to_dict(), ...]``，禁止取数、禁止调 analyze、
禁止 import czsc。跨层契约：字典键来自 ``mystery/core/models.py``。
"""
