# Ming-3.0-tts-0.5B (BF16 GGUF, audio-cpp/Ming-3.0-TTS-0.5B-GGUF @ 2eb8d37, verified) via audio.cpp v0.9.5.
# m1/m2: freeform direction with no reference clip (the instruction chooses voice and delivery).
# m3/m4: the "emotion" template with label 恐惧 (fearful), cloning the male synthetic reference.
# Not "Stop": PowerShell 5.1 turns a native program's stderr lines into error records.
$ErrorActionPreference = "Continue"
$cli = "D:\work\shotwright\spikes\26_drama3_small\_work\runtime\bin\audiocpp_cli.exe"
$model = "D:\work\shotwright\spikes\27_ming3_tts\_work\model\ming-3.0-tts-0.5b-bf16.gguf"
$refs = "D:\work\shotwright\spikes\26_drama3_small\_work\refs"
$refText = Get-Content -Raw "$refs\male_ref.txt"
$out = "D:\work\shotwright\_work\listen\ming3"
New-Item -ItemType Directory -Force $out | Out-Null

$hiding = "[Breathing] Don't move. Don't make a sound. It's right outside the door... I can hear it breathing. If it hears us, it'll find us."
$panic = "No, no, no, it's in here. It's in the room with me! Why won't the door open? Please... please, somebody help me."
$hidingDirection = "A man in his thirties hiding in a dark closet from something that is hunting him. He whispers in terror, barely audible, his breath shaking, pausing to listen between phrases."
$panicDirection = "A man in his thirties in a blind panic, trapped in a room with something terrible. His voice cracks and breaks with fear, rushed and breathless, close to tears, pleading."

# Built from code points: Windows PowerShell 5.1 reads a BOM-less script as ANSI, which
# garbled a literal Chinese label (verified 2026-11-17: the output matched a mojibake label).
$fearful = [string][char]0x6050 + [char]0x60E7   # "fearful", the emotion template's label

$takes = @(
    @{ name = "m1_hiding_freeform"; text = $hiding; options = @("template_name=freeform", "instruct=$hidingDirection") ; clone = $false },
    @{ name = "m2_panic_freeform";  text = $panic;  options = @("template_name=freeform", "instruct=$panicDirection")  ; clone = $false },
    @{ name = "m3_hiding_fearful_clone"; text = $hiding; options = @("template_name=emotion", "emotion=$fearful"); clone = $true },
    @{ name = "m4_panic_fearful_clone";  text = $panic;  options = @("template_name=emotion", "emotion=$fearful"); clone = $true }
)

foreach ($t in $takes) {
    $arguments = @("--family", "ming3_tts", "--backend", "cuda", "--model", $model, "--text", $t.text,
                   "--out", "$out\$($t.name).wav", "--request-option", "language=en")
    if ($t.clone) {
        $arguments += @("--task", "clon", "--voice-ref", "$refs\male_ref.wav", "--request-option", "reference_text=$refText")
    } else {
        $arguments += @("--task", "tts")
    }
    foreach ($o in $t.options) { $arguments += @("--request-option", $o) }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $cli @arguments 2>&1 | Out-File -Encoding utf8 "$out\$($t.name).log"
    "{0,-26} rc={1} seconds={2:N0}" -f $t.name, $LASTEXITCODE, $sw.Elapsed.TotalSeconds
}
