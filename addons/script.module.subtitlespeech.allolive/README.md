# Subtitle realignment: speech

Silero's voice activity model, for the subtitle realignment of the allolive CoreELEC build.
When a subtitle file sits beside a video with no subtitle track of its own, Kodi hears where
someone speaks in the video's sound and moves the file's lines onto it.

`speech.txt` names the model: Silero VAD's ONNX file and its licence at a release tag, by
sha256. `build.sh` fetches them and takes the 16 kHz network's weights out of the ONNX file
(`scripts/speech-weights.py`) into `resources/subtitlespeech/silero_vad_16k.bin` - float32,
little endian, in the order Kodi's `CSpeechDetector` reads them - and checks the result
against its sha256. Nothing of Silero's is stored in this repository.
