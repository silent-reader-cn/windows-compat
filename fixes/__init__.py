"""windows-compat fixes subpackage — each fix is an independent module.

Module contract:
  PRIORITY: int        — recommended enable order (1 = first / most essential)
  RISK: str            — "low" | "medium" | "high"
  DESCRIPTION: str     — what the fix does and why (shown in README/config)
  install() -> bool    — apply the patch; return False if deferred (retry later)
"""
