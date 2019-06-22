import atexit
import bisect
import logging
import numpy
import sounddevice
import threading


logger = logging.getLogger(__name__)


class Track(object):
    """Represents a track being recorded.
    """
    def __init__(self, stream):
        self.waveforms = []
        self.waveforms_offsets = []
        self.live_muted = False
        self.play_muted = False
        self.stream = stream

    def append(self, buf, pos):
        if not self.waveforms:
            # Create first waveform item starting now
            waveform = []
            self.waveforms.append(waveform)
            self.waveforms_offsets.append(pos)
        else:
            # Look at the last waveform item
            waveform = self.waveforms[-1]
            start = self.waveforms_offsets[-1]
            end = start + len(waveform)
            assert pos >= end
            if pos - end < 5:
                # We are right after this item, keep filling it up
                waveform.extend([0 for _ in range(pos - end)])
            else:
                # We are far from the end of this item, make a new one
                waveform = []
                self.waveforms.append(waveform)
                self.waveforms_offsets.append(pos)
        waveform.append(numpy.max(numpy.abs(buf)))

    def get_waveform_at(self, pos):
        idx = bisect.bisect(self.waveforms_offsets, pos)
        if idx >= len(self.waveforms):
            return None
        waveform = self.waveforms[idx]
        start = self.waveforms_offsets[idx]
        if start <= pos < start + len(waveform):
            return waveform
        else:
            return None

    def close(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None


class AudioMixer(object):
    """The audio recording and mixing backend.

    Also computes waveforms.
    """
    def __init__(self, rate=44100, chunk=1024):
        self.rate = rate
        self.chunk = chunk
        self.live = True
        self.closed = False

        self.pos = 0
        self.tracks = set()
        self.output_buf = numpy.zeros((chunk, 1), dtype=numpy.int16)

        self.output_stream = sounddevice.OutputStream(
            channels=1, dtype=numpy.int16,
            samplerate=self.rate, blocksize=self.chunk,
        )
        self.output_stream.start()

        self.recording = False
        self.reading_thread = threading.Thread(target=self._read_write_loop)
        self.reading_thread.setDaemon(True)
        self.reading_thread.start()

        atexit.register(self.close)

    def _read_write_loop(self):
        overflow_timeout = 0
        underflow_timeout = 0
        while not self.closed:
            # Zero buffer
            self.output_buf[:] = 0

            append_buf = []

            for track in self.tracks:
                # Read input
                frames, overflowed = track.stream.read(self.chunk)
                assert frames.shape == (self.chunk, 1)

                if overflow_timeout:
                    overflow_timeout -= 1
                    if overflow_timeout == 0:
                        logger.warning("Input overflowed")
                elif overflowed:
                    overflow_timeout = 100

                # Mix
                if self.live and not track.live_muted:
                    self.output_buf += frames

                if self.recording:
                    # We delay calling append() until after we wrote the output
                    # (for latency reasons)
                    append_buf.append((track, frames))

            # Write output
            underflowed = self.output_stream.write(self.output_buf)
            if underflow_timeout:
                underflow_timeout -= 1
                if underflow_timeout == 0:
                    logger.warning("Output underflowed")
            elif underflowed:
                underflow_timeout = 100

            # If underflowing, write some zeros to catch up
            if underflowed:
                fill = self.output_stream.write_available
                if fill:
                    self.output_stream.write(numpy.zeros(fill,
                                                         dtype=numpy.int16))

            if self.recording:
                for track, frames in append_buf:
                    track.append(frames, self.pos)
                self.pos += 1

    def record(self, recording):
        self.recording = recording

    def new_track(self):
        stream = sounddevice.InputStream(
            channels=1, dtype=numpy.int16,
            samplerate=self.rate, blocksize=self.chunk,
        )
        stream.start()
        track = Track(stream)
        self.tracks.add(track)
        return track

    def close(self):
        if not self.closed:
            self.closed = True
            self.reading_thread.join()

            self.output_stream.stop()
            self.output_stream.close()

            for track in self.tracks:
                track.close()
