let audioContext;
let soundEnabled = true;

export async function unlockGameAudio() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;
  audioContext ||= new AudioContextClass();
  if (audioContext.state === "suspended") {
    await audioContext.resume().catch((error) => {
      console.warn("Не удалось включить игровые звуки.", error);
    });
  }
}

export function setGameSoundEnabled(enabled) {
  soundEnabled = Boolean(enabled);
}

export function isGameSoundEnabled() {
  return soundEnabled;
}

export function playGameSound(kind = "card") {
  if (!soundEnabled || !audioContext || audioContext.state !== "running") return;
  const notes = {
    card: [520, 390],
    win: [523, 659, 784],
    join: [440, 554],
  }[kind] || [520, 390];
  const start = audioContext.currentTime;
  notes.forEach((frequency, index) => {
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();
    const time = start + index * 0.085;
    oscillator.type = "triangle";
    oscillator.frequency.setValueAtTime(frequency, time);
    gain.gain.setValueAtTime(0.0001, time);
    gain.gain.exponentialRampToValueAtTime(0.11, time + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, time + 0.13);
    oscillator.connect(gain);
    gain.connect(audioContext.destination);
    oscillator.start(time);
    oscillator.stop(time + 0.14);
  });
}
