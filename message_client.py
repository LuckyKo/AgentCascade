import requests
import json
import base64
import os
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class AgentCascadeClient:
    def __init__(self, base_url="http://127.0.0.1:8765"):
        self.base_url = base_url.rstrip('/')
        self.session_token = None
        self.shared_secret = None
        
        # Client keys
        self.private_key = x25519.X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.public_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )

    def handshake(self):
        print(f"Connecting to {self.base_url}...")
        try:
            # 1. Get server public key
            resp = requests.get(f"{self.base_url}/api/keys")
            resp.raise_for_status()
            server_pub_b64 = resp.json()["public_key"]
            server_pub_bytes = base64.b64decode(server_pub_b64)
            server_public_key = x25519.X25519PublicKey.from_public_bytes(server_pub_bytes)
            
            # 2. Derive shared secret
            self.shared_secret = self.private_key.exchange(server_public_key)
            
            # 3. Perform handshake
            my_pub_b64 = base64.b64encode(self.public_bytes).decode('utf-8')
            resp = requests.post(f"{self.base_url}/api/handshake", json={"public_key": my_pub_b64})
            resp.raise_for_status()
            self.session_token = resp.json()["session_token"]
            
            print(f"[OK] Handshake successful. Session: {self.session_token[:8]}...")
            return True
        except Exception as e:
            print(f"[ERROR] Handshake failed: {e}")
            return False

    def send_message(self, text, target="Maine"):
        if not self.session_token or not self.shared_secret:
            print("[ERROR] Not connected. Call handshake() first.")
            return False
            
        try:
            # Prepare payload
            payload = json.dumps({"text": text, "target": target}).encode('utf-8')
            
            # Encrypt with AES-GCM
            aesgcm = AESGCM(self.shared_secret)
            nonce = os.urandom(12)
            ciphertext = aesgcm.encrypt(nonce, payload, None)
            
            # Send to server
            data = {
                "session_token": self.session_token,
                "payload": base64.b64encode(ciphertext).decode('utf-8'),
                "nonce": base64.b64encode(nonce).decode('utf-8')
            }
            
            resp = requests.post(f"{self.base_url}/api/message", json=data)
            resp.raise_for_status()
            result = resp.json()
            print(f"[OK] Message queued for {result.get('target')}")
            return True
        except Exception as e:
            print(f"[ERROR] Failed to send message: {e}")
            return False

    def get_status(self):
        if not self.session_token:
            return None
        try:
            resp = requests.get(f"{self.base_url}/api/status", params={"token": self.session_token})
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

def main():
    print("=" * 60)
    print("AgentCascade E2E Encrypted CLI Client")
    print("=" * 60)
    
    url = input("Server URL [http://127.0.0.1:8765]: ").strip() or "http://127.0.0.1:8765"
    client = AgentCascadeClient(url)
    
    if not client.handshake():
        return

    target = "Maine"
    print(f"\nDefault target: {target}")
    print("Commands:")
    print("  /target <name>  - Change target agent")
    print("  /status         - Show server status")
    print("  /exit           - Exit")
    print("-" * 30)

    while True:
        try:
            line = input(f"[{target}] > ").strip()
            if not line:
                continue
                
            if line.startswith("/exit"):
                break
            elif line.startswith("/target "):
                target = line[8:].strip()
                print(f"Target changed to: {target}")
            elif line.startswith("/status"):
                status = client.get_status()
                if status:
                    print(json.dumps(status, indent=2))
                else:
                    print("[ERROR] Could not fetch status.")
            else:
                client.send_message(line, target)
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[ERROR] {e}")

    print("\nGoodbye!")

if __name__ == "__main__":
    main()
