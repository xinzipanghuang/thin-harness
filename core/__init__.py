"""Minimal Codex-style agent runtime.

Design: minimal loop, rich tools, thin harness, strong environment.
The model decides what to do; Python provides capabilities and reliability
boundaries.
"""

from .skill import SkillDefinition, SkillRegistry, register_skill

__all__ = ["SkillDefinition", "SkillRegistry", "register_skill"]
