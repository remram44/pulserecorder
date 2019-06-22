import atexit
import itertools
import logging
import os
import pulsectl
from qtpy import QtCore, QtGui, QtWidgets
import sys
import time

from . import audio


logger = logging.getLogger('pulserecorder')


# Connect to pulseaudio
pulse = pulsectl.Pulse('pulse-recorder-gui')


def get_icon(name):
    if name in get_icon.cache:
        return get_icon.cache[name]

    logging.info("Getting icon %s" % name)
    if not os.path.isabs(name):
        icon = QtGui.QIcon.fromTheme(name)
    else:
        icon = QtGui.QIcon(name)
    get_icon.cache[name] = icon
    return icon


get_icon.cache = {}


class PulseRecorder(QtWidgets.QWidget):
    def __init__(self):
        super(PulseRecorder, self).__init__()

        # Create UI
        layout = QtWidgets.QVBoxLayout()

        # The buttons
        buttons = QtWidgets.QHBoxLayout()
        self.button_record = QtWidgets.QPushButton("rec")
        self.button_record.clicked.connect(lambda: self.record(True))
        buttons.addWidget(self.button_record)
        self.button_stop = QtWidgets.QPushButton("stop")
        self.button_stop.clicked.connect(lambda: self.record(False))
        self.button_stop.setEnabled(False)
        buttons.addWidget(self.button_stop)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        main = QtWidgets.QHBoxLayout()

        # The source picker on the left
        self.sources = QtWidgets.QWidget()
        self.sources.setLayout(QtWidgets.QVBoxLayout())
        self.sources.layout().addWidget(QtWidgets.QLabel("Initializing..."))
        self.sources.layout().addStretch(1)

        scroll = QtWidgets.QScrollArea(widgetResizable=True)
        scroll.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.AdjustToContents)
        scroll.setWidget(self.sources)
        main.addWidget(scroll, 1)

        # The scrollable area with waveforms
        self.tracks_map = {}
        self.tracks = QtWidgets.QWidget()
        scroll_layout = QtWidgets.QVBoxLayout()
        scroll_layout.addWidget(QtWidgets.QLabel("Add a track on the left"))
        scroll_layout.addStretch(1)
        self.tracks.setLayout(scroll_layout)

        scroll = QtWidgets.QScrollArea(widgetResizable=True)
        scroll.setWidget(self.tracks)
        main.addWidget(scroll, 2)

        layout.addLayout(main)

        self.setLayout(layout)

        # Find our own output so we can hide it
        self.ignored_inputs = set()
        old_inputs = set(pb.index for pb in pulse.sink_input_list())
        self.audio_mixer = audio.AudioMixer()
        time.sleep(0.5)
        for pb in pulse.sink_input_list():
            if pb.index not in old_inputs:
                self.ignored_inputs.add(pb.index)

        # Set timer to refresh sources
        timer = QtCore.QTimer(self)
        timer.setSingleShot(False)
        timer.timeout.connect(self.refresh_sources)
        self.refresh_sources()
        timer.start(2000)

    def sizeHint(self):
        return QtCore.QSize(500, 300)

    def record(self, recording):
        self.audio_mixer.record(recording)
        self.button_record.setEnabled(not recording)
        self.button_stop.setEnabled(recording)

    def refresh_sources(self):
        # Find all listeners using pulsectl
        disconnected = dict(self.tracks_map)
        apps = []
        for out in pulse.sink_input_list():
            if out.index in self.ignored_inputs:
                continue
            app = {'idx': out.index}
            if ('application.name' in out.proplist and
                    'application.process.binary' in out.proplist):
                app['name'] = '%s (%s)' % (
                    out.proplist['application.process.binary'],
                    out.proplist['application.name'],
                )
            elif 'application.process.binary' in out.proplist:
                app['name'] = out.proplist['application.process.binary']
            elif 'application.name' in out.proplist:
                app['name'] = out.proplist['application.name']
            else:
                app['name'] = 'unknown'
            if 'application.icon_name' in out.proplist:
                app['icon'] = out.proplist['application.icon_name']

            if app['name'] in self.tracks_map:
                disconnected.pop(app['name'], None)
                continue

            apps.append(app)

        # Mark disconnected tracks
        for name, track in disconnected.items():
            if track.connected:
                track.disconnect()
            self.tracks_map.pop(name, None)

        # TODO: Handle reconnections

        # Update the list
        layout = self.sources.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if apps:
            for app in sorted(apps, key=lambda a: a.get('name', '')):
                button = QtWidgets.QPushButton(app['name'])
                if 'icon' in app:
                    icon = get_icon(app['icon'])
                    button.setIcon(icon)
                button.clicked.connect(lambda: self.add_source(app))
                layout.addWidget(button)
        else:
            layout.addWidget(QtWidgets.QLabel("No application playing sound"))

    def add_source(self, app):
        layout = self.tracks.layout()
        if not self.tracks_map:
            # Remove "no tracks" label
            layout.takeAt(0).widget().deleteLater()
        widget = Track(app, self.audio_mixer)
        layout.insertWidget(self.tracks.layout().count() - 1, widget)
        self.tracks_map[app['name']] = widget
        logger.info("Added track %s", app['name'])
        self.refresh_sources()


def create_nullsink():
    # Identify defaults. They might switch to the new module, and we'll restore
    default_in = pulse.server_info().default_source_name
    default_out = pulse.server_info().default_sink_name

    # Load
    mod = pulse.module_load(
        'module-null-sink',
        args='sink_properties=device.description=pulserecorder',
    )
    _clear_nullsinks.mods.add(mod)
    time.sleep(0.5)
    nullsink, = [sink
                 for sink in pulse.sink_list()
                 if sink.owner_module == mod]
    nullmonitor, = [source
                    for source in pulse.source_list()
                    if source.name == nullsink.monitor_source_name]

    # Restore defaults
    pulse.source_default_set(default_in)
    pulse.sink_default_set(default_out)

    # Set volumes
    pulse.volume_set_all_chans(nullsink, 1.0)
    pulse.volume_set_all_chans(nullmonitor, 1.0)

    return nullsink, nullmonitor


@atexit.register
def _clear_nullsinks():
    for mod in _clear_nullsinks.mods:
        pulse.module_unload(mod)


_clear_nullsinks.mods = set()


class Track(QtWidgets.QGroupBox):
    def __init__(self, app, audio_mixer):
        super(Track, self).__init__(app['name'])
        self.app = app
        self.connected = True

        # Wire up the app to a pulseaudio nullsink
        nullsink, nullmonitor = create_nullsink()
        pulse.sink_input_move(app['idx'], nullsink.index)

        # Create recording stream
        old_outputs = set(rec.index for rec in pulse.source_output_list())
        self.audio_track = audio_mixer.new_track()
        time.sleep(0.5)

        # Wire up the recording stream to the pulseaudio nullsink monitor
        for rec in pulse.source_output_list():
            if rec.index in old_outputs:
                pass
            pulse.source_output_move(rec.index, nullmonitor.index)
            break
        else:
            assert 0, "Couldn't set up recording"

        # Create UI
        layout = QtWidgets.QVBoxLayout()
        self.waveform = Waveform(self.audio_track)
        layout.addWidget(self.waveform)
        self.setLayout(layout)

    def disconnect(self):
        self.connected = False
        logger.info("Track %s disconnected", self.app['name'])


class Waveform(QtWidgets.QWidget):
    def __init__(self, audio_track):
        super(Waveform, self).__init__()
        self.audio_track = audio_track
        timer = QtCore.QTimer(self)
        timer.setSingleShot(False)
        timer.timeout.connect(lambda: self.update(self.visibleRegion()))
        timer.start(100)

    def minimumSizeHint(self):
        return QtCore.QSize(100, 100)

    def sizeHint(self):
        return QtCore.QSize(300, 100)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        for re in event.region().rects():
            painter.fillRect(re, QtGui.QColor(200, 200, 255))
        painter.setBrush(QtGui.QColor(0, 0, 255))
        for waveform, start in zip(self.audio_track.waveforms,
                                   self.audio_track.waveforms_offsets):
            painter.drawPolygon(self.poly(itertools.chain(
                ((pos, 50.0 - value / 32768.0 * 50.0)
                 for pos, value in enumerate(waveform, start)),
                ((start + i, 50.0 + waveform[i] / 32768.0 * 50.0)
                 for i in reversed(range(len(waveform))))
            )))
        painter.end()

    @staticmethod
    def poly(points):
        return QtGui.QPolygon([QtCore.QPoint(*p) for p in points])


def main():
    logging.basicConfig(level=logging.INFO)

    app = QtWidgets.QApplication(sys.argv)
    window = PulseRecorder()
    window.setVisible(True)
    app.exec_()
