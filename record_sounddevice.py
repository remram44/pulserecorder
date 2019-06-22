import numpy
import sounddevice


RATE = 44100
CHUNK = 1024


dev = sounddevice.InputStream(channels=2, samplerate=RATE, dtype=numpy.int16,
                              blocksize=CHUNK)

frames = []
dev.start()
for i in range(int(RATE / CHUNK * 5)):
    buf, overflowed = dev.read(CHUNK)
    frames.append(buf)
    print(".", end='', flush=True)
dev.stop()
dev.close()

with open('out.raw', 'wb') as fp:
    for buf in frames:
        buf.tofile(fp)
