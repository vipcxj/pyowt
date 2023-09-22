import hmac
from hashlib import sha256
import base64

class Auth:
    
    realm: str
    signature_method: str
    username: str
    role: str
    service_id: str
    service_key: str
    
    
    def __init__(self, service_id: str, service_key: str, username: str = '', role: str = '') -> None:
        self.realm = 'http://webrtc.intel.com'
        self.signature_method = 'HMAC_SHA256'
        self.service_id = service_id
        self.username = username
        self.role = role
        
    def hmac_sha256(self, to_sign: str, key: str):
        hash = hmac.new(base64.b64decode(key), to_sign.encode(), sha256)
        signed = base64.b64encode(hash.digest())
        return signed.decode()
    
    def sign(self, timestamp: int, nonce: int) -> str:
        to_sign = f'{timestamp},{nonce}'
        if self.username and self.role:
            to_sign += f',{self.username},{self.role}'
        return self.hmac_sha256(to_sign, self.service_key)