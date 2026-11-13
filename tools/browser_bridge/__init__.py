"""Explicit local web-agent bridges; no generic browser access."""
from .muse import MuseBridge, MuseBridgeError

__all__ = ['MuseBridge', 'MuseBridgeError']
