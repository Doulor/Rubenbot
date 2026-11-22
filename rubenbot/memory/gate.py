"""兼容旧版导入；分寸裁定的实现已移至核心隐私模块。"""

from ..privacy.gate import OWNER_AUDIENCE, PrivacyGate, ReviewItem, audience_kind, has_high_sensitivity_hint, ruling_key

__all__ = ["OWNER_AUDIENCE", "PrivacyGate", "ReviewItem", "audience_kind", "has_high_sensitivity_hint", "ruling_key"]
