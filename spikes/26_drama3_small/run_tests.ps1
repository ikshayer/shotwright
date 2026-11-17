# Drama3-small horror test: three lines cloned from the male synthetic reference.
# Runtime: audio.cpp v0.9.5 (windows-x64-cuda13.3, checksums verified); model:
# pentacoxian-dev/drama3-small-GGUF @ 4a3078e (checksums verified).
# Outputs land in D:\work\shotwright\_work\listen\drama3_small\ (gitignored).

# Not "Stop": PowerShell 5.1 turns a native program's stderr lines into error records,
# and audiocpp_cli logs normal progress to stderr. Failures are read from the exit code.
$ErrorActionPreference = "Continue"
$work = "D:\work\shotwright\spikes\26_drama3_small\_work"
$cli = "$work\runtime\bin\audiocpp_cli.exe"
$model = "$work\model\drama3-small-IQ3_XXS.gguf"
$ref = "$work\refs\male_ref.wav"
$refText = Get-Content -Raw "$work\refs\male_ref.txt"
$out = "D:\work\shotwright\_work\listen\drama3_small"
New-Item -ItemType Directory -Force $out | Out-Null

$lines = [ordered]@{
    "1_hiding_directed" = "[whispering, terrified, barely breathing] Don't move. Don't make a sound. [gasps] It's right outside the door... I can hear it breathing. <whisper>If it hears us, it'll find us.</whisper>"
    "2_panic_directed"  = "[panicked, voice breaking] No, no, no, it's in here. It's in the room with me! [sobs] Why won't the door open? Please... please, somebody help me."
    "3_hiding_plain"    = "Don't move. Don't make a sound. It's right outside the door, I can hear it breathing. If it hears us, it'll find us."
}

$seed = 8000
foreach ($name in $lines.Keys) {
    $seed++
    $wav = "$out\$name.wav"
    $log = "$out\$name.log"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $cli --task tts --family fish_drama3 --backend cuda `
        --model $model --voice-ref $ref --text $lines[$name] --out $wav --threads 8 `
        --request-option "reference_text=$refText" `
        --request-option temperature=0.7 --request-option top_p=0.7 --request-option top_k=30 `
        --request-option seed=$seed --request-option max_tokens=1024 `
        --session-option moe_cpu_expert_layers=8 `
        --session-option fish_drama3.context_length=2048 *> $log
    "{0,-20} rc={1} seconds={2:N0}" -f $name, $LASTEXITCODE, $sw.Elapsed.TotalSeconds
}
Get-ChildItem $out -Filter *.wav | Select-Object Name, Length
