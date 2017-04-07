import abc
import six
import gevent
import struct
import subprocess


from gevent.queue import Queue

from disco.voice.opus import OpusEncoder


try:
    from cStringIO import cStringIO as StringIO
except:
    from StringIO import StringIO


OPUS_HEADER_SIZE = struct.calcsize('<h')


# Play from file:
#  OpusFilePlayable(open('myfile.opus', 'r'))
#  PCMFileInput(open('myfile.pcm', 'r')).pipe(DCADOpusEncoder) => OpusPlayable
#  FFMpegInput.youtube_dl('youtube.com/yolo').pipe(DCADOpusEncoder) => OpusPlayable
#  FFMpegInput.youtube_dl('youtube.com/yolo').pipe(OpusEncoder).pipe(DuplexStream, open('cache_file.opus', 'w')) => OpusPlayable


class AbstractOpus(object):
    def __init__(self, sampling_rate=48000, frame_length=20, channels=2):
        self.sampling_rate = sampling_rate
        self.frame_length = frame_length
        self.channels = 2
        self.sample_size = 2 * self.channels
        self.samples_per_frame = int(self.sampling_rate / 1000 * self.frame_length)
        self.frame_size = self.samples_per_frame * self.sample_size


@six.add_metaclass(abc.ABCMeta)
class BasePlayable(object):
    @abc.abstractmethod
    def next_frame(self):
        raise NotImplementedError


@six.add_metaclass(abc.ABCMeta)
class BaseInput(object):
    @abc.abstractmethod
    def read(self, size):
        raise NotImplementedError

    @abc.abstractmethod
    def fileobj(self):
        raise NotImplementedError

    def pipe(self, other, *args, **kwargs):
        return other(self, *args, **kwargs)


class OpusFilePlayable(BasePlayable):
    """
    An input which reads opus data from a file or file-like object.
    """
    def __init__(self, fobj):
        self.fobj = fobj
        self.done = False

    def next_frame(self):
        if self.done:
            return None

        header = self.fobj.read(OPUS_HEADER_SIZE)
        if len(header) < OPUS_HEADER_SIZE:
            self.done = True
            return None

        data_size = struct.unpack('<h', header)[0]
        data = self.fobj.read(data_size)
        if len(data) < data_size:
            self.done = True
            return None

        return data


class FFmpegInput(BaseInput, AbstractOpus):
    def __init__(self, source='-', command='avconv', streaming=False, **kwargs):
        super(FFmpegInput, self).__init__(**kwargs)
        self.streaming = streaming
        self.source = source
        self.command = command

        self._buffer = None
        self._proc = None

    @classmethod
    def youtube_dl(cls, url, *args, **kwargs):
        import youtube_dl

        ydl = youtube_dl.YoutubeDL({'format': 'webm[abr>0]/bestaudio/best'})
        info = ydl.extract_info(url, download=False)

        if 'entries' in info:
            info = info['entries'][0]

        result = cls(source=info['url'], *args, **kwargs)
        result.info = info
        return result

    def read(self, sz):
        if self.streaming:
            raise TypeError('Cannot read from a streaming FFmpegInput')

        # First read blocks until the subprocess finishes
        if not self._buffer:
            data, _ = self.proc.communicate()
            self._buffer = StringIO(data)

        # Subsequent reads can just do dis thang
        return self._buffer.read(sz)

    def fileobj(self):
        if self.streaming:
            return self.proc.stdout
        else:
            return self

    @property
    def proc(self):
        if not self._proc:
            args = [
                self.command,
                '-i', self.source,
                '-f', 's16le',
                '-ar', str(self.sampling_rate),
                '-ac', str(self.channels),
                '-loglevel', 'warning',
                'pipe:1'
            ]
            self._proc = subprocess.Popen(args, stdin=None, stdout=subprocess.PIPE)
        return self._proc


class BufferedOpusEncoderPlayable(BasePlayable, AbstractOpus, OpusEncoder):
    def __init__(self, source, *args, **kwargs):
        self.source = source
        self.frames = Queue(kwargs.pop('queue_size', 4096))
        super(BufferedOpusEncoderPlayable, self).__init__(*args, **kwargs)
        gevent.spawn(self._encoder_loop)

    def _encoder_loop(self):
        while self.source:
            raw = self.source.read(self.frame_size)
            if len(raw) < self.frame_size:
                break

            self.frames.put(self.encode(raw, self.samples_per_frame))
            gevent.idle()
        self.source = None

    def next_frame(self):
        if not self.source:
            return None
        return self.frames.get()


class DCADOpusEncoderPlayable(BasePlayable, AbstractOpus, OpusEncoder):
    def __init__(self, source, *args, **kwargs):
        self.source = source
        self.command = kwargs.pop('command', 'dcad')
        super(DCADOpusEncoderPlayable, self).__init__(*args, **kwargs)

        self._done = False
        self._proc = None

    @property
    def proc(self):
        if not self._proc:
            source = obj = self.source.fileobj()
            if not hasattr(obj, 'fileno'):
                source = subprocess.PIPE

            self._proc = subprocess.Popen([
                self.command,
                '--channels', str(self.channels),
                '--rate', str(self.sampling_rate),
                '--size', str(self.samples_per_frame),
                '--bitrate', '128',
                '--fec',
                '--packet-loss-percent', '30',
                '--input', 'pipe:0',
                '--output', 'pipe:1',
            ], stdin=source, stdout=subprocess.PIPE)

            def writer():
                while True:
                    data = obj.read(2048)
                    if data > 0:
                        self._proc.stdin.write(data)
                    if data < 2048:
                        break

            if source == subprocess.PIPE:
                gevent.spawn(writer)
        return self._proc

    def next_frame(self):
        if self._done:
            return None

        header = self.proc.stdout.read(OPUS_HEADER_SIZE)
        if len(header) < OPUS_HEADER_SIZE:
            self._done = True
            return

        size = struct.unpack('<h', header)[0]

        data = self.proc.stdout.read(size)
        if len(data) == 0:
            self._done = True
            return

        return data