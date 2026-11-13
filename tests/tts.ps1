# Speak a text into a WAV file with a Windows voice of the given language (WinRT voices: English, Vietnamese...)
# usage: powershell -File tests\tts.ps1 <lang e.g. en-US / vi-VN> <out.wav> <text file (UTF-8)>
param([string]$Lang, [string]$Out, [string]$TextFile)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait() | Out-Null
    $task.Result
}
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.Language -eq $Lang } | Select-Object -First 1
if (-not $voice) { throw "no voice for $Lang" }
$synth.Voice = $voice
$stream = Await ($synth.SynthesizeTextToStreamAsync($text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
$reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
$n = [uint32]$stream.Size
$null = Await ($reader.LoadAsync($n)) ([uint32])
$bytes = New-Object byte[] $n
$reader.ReadBytes($bytes)
[System.IO.File]::WriteAllBytes($Out, $bytes)
Write-Output "ok $($voice.DisplayName) $n bytes"
