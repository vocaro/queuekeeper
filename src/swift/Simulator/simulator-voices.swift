// The system's voices' first use in an iOS simulator, made and timed by a program `xcrun simctl spawn` runs
// there, as a test process makes it: the voices' list, then each language's own voice (vocaro/EPUBLib#946,
// vocaro-engineering#172). The pool's template warm-up builds it and runs it twice, with a reboot between
// (vocaro_engineering.simulators.template). Each line is the seconds since the program started and what it
// did; it exits 1 when the system lists no voice.
import AVFAudio
import Foundation

let started = Date()
func say(_ what: String) {
    print(String(format: "%.3f %@", Date().timeIntervalSince(started), what))
    fflush(stdout)
}

say("start pid \(getpid())")
let voices = AVSpeechSynthesisVoice.speechVoices()
say("speechVoices \(voices.count)")
let languages = Array(Set(voices.map(\.language))).sorted()
let own = languages.compactMap { AVSpeechSynthesisVoice(language: $0) }
say("languages \(own.count) of \(languages.count) have their own voice")
exit(voices.isEmpty ? 1 : 0)
