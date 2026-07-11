#!/usr/bin/env python3
"""Test consecutive auto-ask security check chaining via WebSocket."""
import json, websockets, asyncio, time

async def test_auto_ask_chain():
    uri = "ws://host.docker.internal:12346/ws/chat"
    log = []
    
    async with websockets.connect(uri, ping_interval=None, max_size=8*1024*1024) as ws:
        # Get initial state
        init = await asyncio.wait_for(ws.recv(), timeout=8)
        d = json.loads(init)
        aps = d.get('approvals', [])
        t0 = time.monotonic()
        log.append(f"T+{0:.1f}s INIT: {len(aps)} pending approvals")
        for a in aps[:5]:
            log.append(f"  rid={a['request_id']} tool={a.get('tool_name','?')}")
        
        # Collect messages for 40s with individual timeouts
        deadline = t0 + 40
        msg_count = 0
        
        while time.monotonic() < deadline and msg_count < 50:
            remaining = min(deadline - time.monotonic(), 8)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                d = json.loads(raw)
                mt = d.get('type','?')
                el = f"{time.monotonic()-t0:.1f}"
                
                if mt == 'approvals':
                    aps2 = d.get('approvals', [])
                    rids = [a['request_id'] for a in aps2[:3]]
                    log.append(f"T+{el}s APPROVALS({len(aps2)}): {rids}")
                elif mt == 'security_response':
                    log.append(f"T+{el}s SEC_RESPONSE: rid={d.get('request_id','?')} verdict={d.get('verdict','?')}")
                
                msg_count += 1
            except asyncio.TimeoutError:
                break
        
        # Summary
        if not any("SEC_RESPONSE" in l for l in log):
            log.append("\nNo security responses received — agent may be idle or no pending approvals.")
    
    return "\n".join(log)

if __name__ == "__main__":
    result = asyncio.run(test_auto_ask_chain())
    print(result)