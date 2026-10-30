"""Reuse public collectors with Helium's own optional settings and state."""
from sniper.krypton.providers import Providers as KryptonProviders, ProviderDeferred, settings


class Providers(KryptonProviders):
    settings_folder = 'helium_bot'
    agent_name = 'Helium'
