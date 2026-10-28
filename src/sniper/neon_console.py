"""Compatibility entry points for the shared read-only Solana paper adapter."""
import time
from .solana_console import view as shared_view, chart as shared_chart


def view(root):
    return shared_view(root, 'neon', int(time.time()))


def chart(root, asset, pool):
    return shared_chart(root, 'neon', asset, pool)
