param(
    [int]$AcWsPort = 8126,
    [int]$Delay = 5,
    [string]$TargetAgent = "Maine",
    [int]$HealthCheckTimeout = 30
)

$AcHost = "127.0.0.1"

function SendWsMessage($wsHost, $wsPort, $wsPath, $json) {
    try {
        $socket = New-Object System.Net.Sockets.TcpClient($wsHost, $wsPort)
        $stream = $socket.GetStream()
        
        # Generate WebSocket key and handshake
        $rnd = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
        $keyBytes = New-Object byte[] 16
        $rnd.GetBytes($keyBytes)
        $key = [Convert]::ToBase64String($keyBytes)
        $guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        $sha1 = [System.Security.Cryptography.SHA1]::Create()
        $accept = [Convert]::ToBase64String(
            $sha1.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($key + $guid)))
        
        $upgrade = "GET $wsPath HTTP/1.1`r`nHost: ${wsHost}:${wsPort}`r`nUpgrade: websocket`r`nConnection: Upgrade`r`nSec-WebSocket-Key: $key`r`nSec-WebSocket-Version: 13`r`n`r`n"
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($upgrade)
        $stream.Write($bytes, 0, $bytes.Length)
        
        # Read response
        $responseBytes = New-Object byte[] 1024
        $read = $stream.Read($responseBytes, 0, 1024)
        $response = [System.Text.Encoding]::UTF8.GetString($responseBytes, 0, $read)
        
        if ($response.Contains("101")) {
            # Send WebSocket frame for text message (masked per RFC 6455)
            $payload = [System.Text.Encoding]::UTF8.GetBytes($json)
            $payloadLen = $payload.Length
            
            $maskKey = New-Object byte[] 4
            $rnd.GetBytes($maskKey)
            
            # Frame: FIN+TEXT(0x81), masked+len, mask key (4 bytes), masked payload
            $frame = New-Object byte[] (6 + $payloadLen)
            $frame[0] = 0x81
            $frame[1] = 0x80 + [byte]$payloadLen
            for ($i = 0; $i -lt 4; $i++) { $frame[2+$i] = $maskKey[$i] }
            
            for ($i = 0; $i -lt $payloadLen; $i++) {
                $frame[6 + $i] = $payload[$i] -bxor $maskKey[$i % 4]
            }
            
            $stream.Write($frame, 0, $frame.Length)
        } else {
            Write-Host "WebSocket upgrade failed: $response" -ForegroundColor Red
        }
        
        Start-Sleep -Milliseconds 500
        $socket.Close()
    } catch {
        Write-Host "Error sending WebSocket message: $_" -ForegroundColor Red
    }
}

function Test-AcHealth() {
    # TCP connect check (port open = likely ready)
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect($AcHost, $AcWsPort) | Out-Null
        $tcp.Close()
        return $true
    } catch {
        return $false
    }
}

# === Step 1: Request AgentCascade restart ===
Write-Host "=== Restarting AgentCascade server ===" -ForegroundColor Cyan
SendWsMessage $AcHost $AcWsPort "/ws/chat" '{"type": "restart_server"}'

# === Step 2: Wait for AgentCascade to be back up ===
Write-Host "`nWaiting for AgentCascade to restart (max ${HealthCheckTimeout}s)..." -ForegroundColor Yellow
$elapsed = 0
$interval = 2
while ($elapsed -lt $HealthCheckTimeout) {
    Start-Sleep -Seconds $interval
    $elapsed += $interval
    if (Test-AcHealth) {
        Write-Host "AgentCascade is back up after ${elapsed}s." -ForegroundColor Green
        break
    }
    Write-Host "  ...waiting ($elapsed/${HealthCheckTimeout}s)" -ForegroundColor Gray
}

if ($elapsed -ge $HealthCheckTimeout) {
    Write-Host "Health check timed out after ${HealthCheckTimeout}s, proceeding anyway (fallback delay: ${Delay}s)." -ForegroundColor Yellow
    Start-Sleep -Seconds $Delay
}

# === Step 3: Resume agent with user message ===
Write-Host "`n=== Resuming agent ===" -ForegroundColor Cyan
$json = @"
{"type": "message", "text": "Server restarted. Please continue with your previous task.", "target_agent": "$TargetAgent"}
"@
SendWsMessage $AcHost $AcWsPort "/ws/chat" $json

Write-Host "`nDone. Server restarted and user message sent to $TargetAgent." -ForegroundColor Green