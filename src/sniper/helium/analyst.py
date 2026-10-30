"""The baseline Grok analyst, with Helium-owned requests and authorization."""
from sniper.krypton.grok import Grok as KryptonGrok, validate
from .providers import settings


class Grok(KryptonGrok):
    request_prefix = 'helium:grok'

    def requires_invoice(self,request_id,payload):
        # Jev paper estimates remain visible and charged to risk equity, while
        # the original Grok request/billing requirement remains unchanged.
        return not (request_id.startswith('helium:jev:') and payload.get('provider') == 'jev')

    def key(self):
        key = settings(self.root, 'helium_bot').get('XAI_API_KEY')
        if not key:
            key = (self.root/'runtime/xai-api-key.txt').read_text().strip()
        if not key or key == 'your_key_here' or '\n' in key or '\r' in key:
            raise ValueError('Invalid credential')
        return key
