# Provenance Guard — demo runner
# Rehearsal script: runs the full end-to-end sequence against a locally running server.
# Start the server first in another terminal:  .venv/Scripts/python app.py
# Then:  ./demo.ps1

$ErrorActionPreference = "Stop"
$base = "http://localhost:5000"

function Step($title) {
    Write-Host ""
    Write-Host "==== $title ====" -ForegroundColor Cyan
}

function Post($path, $obj) {
    $body = $obj | ConvertTo-Json -Depth 5
    Invoke-RestMethod -Uri "$base$path" -Method Post -ContentType "application/json" -Body $body
}

Step "1a. Human submission (expect likely_human)"
Post "/submit" @{
    creator_id = "demo"
    text = "ok so i tried that ramen place downtown and honestly? underwhelming. broth was fine but WAY too salty. probably wont go back"
} | ConvertTo-Json -Depth 5

Step "1b. AI submission (expect likely_ai)"
$ai = Post "/submit" @{
    creator_id = "demo"
    text = "The system is good. The system is fast. The system is reliable. The system is good. The system is fast. The system is reliable."
}
$ai | ConvertTo-Json -Depth 5

Step "2. Appeal the AI decision (status -> under_review)"
Post "/appeal" @{
    content_id = $ai.content_id
    creator_reasoning = "I wrote this intentionally as experimental repetitive verse."
} | ConvertTo-Json -Depth 5

Step "3. Multi-modal: image metadata (expect likely_ai)"
Post "/submit" @{
    creator_id = "demo"
    content_type = "image_metadata"
    metadata = @{
        software = "Midjourney v6"
        generation_prompt = "a cat astronaut"
        c2pa = @{ ai_generated = $true }
    }
} | ConvertTo-Json -Depth 5

Step "4. Audit log"
Invoke-RestMethod -Uri "$base/log" | ConvertTo-Json -Depth 6

Step "5. Analytics dashboard"
Invoke-RestMethod -Uri "$base/analytics" | ConvertTo-Json -Depth 5

Write-Host ""
Write-Host "Demo complete." -ForegroundColor Green
