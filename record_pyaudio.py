import pyaudio


FORMAT = pyaudio.paInt16
RATE = 44100
CHUNK = 1024


audio = pyaudio.PyAudio()

stream = audio.open(format=FORMAT,
                    channels=2,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK)
print("Recording", end='', flush=True)
frames = []
for i in range(int(RATE / CHUNK * 5)):
    data = stream.read(CHUNK)
    frames.append(data)
    print(".", end='', flush=True)
print("Recorded 5 seconds")

stream.stop_stream()
stream.close()
audio.terminate()

with open('out.raw', 'wb') as fp:
    fp.write(b''.join(frames))
