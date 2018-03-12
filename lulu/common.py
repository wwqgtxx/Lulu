#!/usr/bin/env python

import io
import os
import re
import sys
import time
import socket
import locale
import logging
import argparse
from urllib import parse
from html import unescape
from http import cookiejar
from importlib import import_module
from multiprocessing.dummy import Pool

import urllib3
import requests

from lulu.config import (
    SITES,
    FAKE_HEADERS,
)
from lulu.util import log, term
from lulu.version import __version__
from lulu import json_output as json_output_
from lulu.util.strings import get_filename

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf8')


dry_run = False
json_output = False
force = False
player = None
extractor_proxy = None
cookies = None
output_filename = None


if sys.stdout.isatty():
    default_encoding = sys.stdout.encoding.lower()
else:
    default_encoding = locale.getpreferredencoding().lower()


# disable SSL verify=False warning
urllib3.disable_warnings()
session = requests.Session()


def rc4(key, data):
    # all encryption algo should work on bytes
    assert type(key) == type(data) and type(key) == type(b'')
    state = list(range(256))
    j = 0
    for i in range(256):
        j += state[i] + key[i % len(key)]
        j &= 0xff
        state[i], state[j] = state[j], state[i]

    i = 0
    j = 0
    out_list = []
    for char in data:
        i += 1
        i &= 0xff
        j += state[i]
        j &= 0xff
        state[i], state[j] = state[j], state[i]
        prn = state[(state[i] + state[j]) & 0xff]
        out_list.append(char ^ prn)

    return bytes(out_list)


def general_m3u8_extractor(url, headers=FAKE_HEADERS):
    m3u8_list = get_content(url, headers=headers).split('\n')
    urls = []
    for line in m3u8_list:
        line = line.strip()
        if line and not line.startswith('#'):
            if line.startswith('http'):
                urls.append(line)
            else:
                seg_url = parse.urljoin(url, line)
                urls.append(seg_url)
    return urls


def maybe_print(*s):
    try:
        print(*s)
    except Exception:
        pass


def tr(s):
    if default_encoding == 'utf-8':
        return s
    else:
        return s
        # return str(s.encode('utf-8'))[2:-1]


def match1(text, *patterns):
    """Scans through a string for substrings matched some patterns
    (first-subgroups only).

    Args:
        text: A string to be scanned.
        patterns: Arbitrary number of regex patterns.

    Returns:
        When only one pattern is given, returns a string
        (None if no match found).
        When more than one pattern are given, returns a list of strings
        ([] if no match found).
    """

    if len(patterns) == 1:
        pattern = patterns[0]
        match = re.search(pattern, text)
        if match:
            return match.group(1)
        else:
            return None
    else:
        ret = []
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                ret.append(match.group(1))
        return ret


def matchall(text, patterns):
    """Scans through a string for substrings matched some patterns.

    Args:
        text: A string to be scanned.
        patterns: a list of regex pattern.

    Returns:
        a list if matched. empty if not.
    """

    ret = []
    for pattern in patterns:
        match = re.findall(pattern, text)
        ret += match

    return ret


def launch_player(player, urls, refer=''):
    import subprocess
    import shlex
    player = shlex.split(player)
    params = []
    ua = FAKE_HEADERS['User-Agent']
    if player[0] == 'mpv':
        params.append('--no-ytdl')
        params.extend(['--user-agent', ua])
        if refer:
            params.extend([
                '--http-header-fields',
                'referer: {}'.format(refer)
            ])
    subprocess.call(player + params + list(urls))


def parse_query_param(url, param):
    """Parses the query string of a URL and returns the value of a parameter.

    Args:
        url: A URL.
        param: A string representing the name of the parameter.

    Returns:
        The value of the parameter.
    """

    try:
        return parse.parse_qs(parse.urlparse(url).query)[param][0]
    except Exception:
        return None


def unicodize(text):
    return re.sub(
        r'\\u([0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f])',
        lambda x: chr(int(x.group(0)[2:], 16)),
        text
    )


def ungzip(data):
    """Decompresses data for Content-Encoding: gzip.
    """
    from io import BytesIO
    import gzip
    buffer = BytesIO(data)
    f = gzip.GzipFile(fileobj=buffer)
    return f.read()


def get_location(url):
    logging.debug('get_location: {}'.format(url))
    response = session.get(url)
    return response.url


def urlopen_with_retry(*args, method='get', **kwargs):
    retry_time = 3
    for i in range(retry_time):
        try:
            return getattr(session, method)(
                *args, stream=True, verify=False, **kwargs
            )
        except requests.Timeout as e:
            logging.debug('request attempt {} timeout'.format(str(i + 1)))
            if i + 1 == retry_time:
                raise e
        # try to tackle youku CDN fails
        except requests.HTTPError as http_error:
            logging.debug('HTTP Error with code{}'.format(
                http_error.response.status_code
            ))
            if i + 1 == retry_time:
                raise http_error


def get_content(url, headers=FAKE_HEADERS):
    """Gets the content of a URL via sending a HTTP GET request.

    Args:
        url: A URL.
        headers: Request headers used by the client.
        decoded: Whether decode the response body using UTF-8 or the charset
        specified in Content-Type.

    Returns:
        The content as a string.
    """

    logging.debug('get_content: {}'.format(url))

    if cookies:
        session.cookies = cookies

    response = urlopen_with_retry(url, headers=headers)
    data = response.text
    return data


def post_content(url, headers=FAKE_HEADERS, post_data={}):
    """Post the content of a URL via sending a HTTP POST request.

    Args:
        url: A URL.
        headers: Request headers used by the client.
        decoded: Whether decode the response body using UTF-8 or the charset
        specified in Content-Type.

    Returns:
        The content as a string.
    """

    logging.debug('post_content: {} \n post_data: {}'.format(url, post_data))

    if cookies:
        session.cookies = cookies

    response = urlopen_with_retry(
        url, method='post', headers=headers, data=post_data
    )
    data = response.text
    return data


def url_size(url, headers=FAKE_HEADERS):
    response = urlopen_with_retry(url, headers=headers)
    size = response.headers['content-length']
    return int(size) if size is not None else float('inf')


def urls_size(urls, headers=FAKE_HEADERS):
    return sum([url_size(url, headers=headers) for url in urls])


def get_head(url, headers=FAKE_HEADERS):
    logging.debug('get_head: {}'.format(url))
    res = urlopen_with_retry(url, headers=headers)
    return res.headers


def url_info(url, headers=FAKE_HEADERS, refer=None):
    logging.debug('url_info: {}'.format(url))
    if refer:
        headers.update({'Referer': refer})
    headers = get_head(url, headers)

    _type = headers['content-type']
    if _type == 'image/jpg; charset=UTF-8' or _type == 'image/jpg':
        _type = 'audio/mpeg'  # fix for netease
    mapping = {
        'video/3gpp': '3gp',
        'video/f4v': 'flv',
        'video/mp4': 'mp4',
        'video/MP2T': 'ts',
        'video/quicktime': 'mov',
        'video/webm': 'webm',
        'video/x-flv': 'flv',
        'video/x-ms-asf': 'asf',
        'audio/mp4': 'mp4',
        'audio/mpeg': 'mp3',
        'audio/wav': 'wav',
        'audio/x-wav': 'wav',
        'audio/wave': 'wav',
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/gif': 'gif',
        'application/pdf': 'pdf',
    }
    if _type in mapping:
        ext = mapping[_type]
    elif '.' in url:
        _type = ext = url.split('.')[-1]
    else:
        _type = None
        if headers['content-disposition']:
            try:
                filename = parse.unquote(
                    match1(
                        headers['content-disposition'], r'filename="?([^"]+)"?'
                    )
                )
                if len(filename.split('.')) > 1:
                    ext = filename.split('.')[-1]
                else:
                    ext = None
            except Exception:
                ext = None
        else:
            ext = None

    if headers.get('transfer-encoding') != 'chunked':
        size = headers['content-length'] and int(headers['content-length'])
    else:
        size = None

    return _type, ext, size


def url_locations(urls, headers=FAKE_HEADERS):
    locations = []
    for url in urls:
        logging.debug('url_locations: %s' % url)

        response = urlopen_with_retry(url, headers=headers)

        locations.append(response.url)
    return locations


def url_save(
    url, filepath, bar, refer=None, is_part=False, headers=None, timeout=None,
    **kwargs
):
    tmp_headers = headers.copy() if headers else FAKE_HEADERS.copy()
    # When a referer specified with param refer,
    # the key must be 'Referer' for the hack here
    if refer:
        tmp_headers['Referer'] = refer
    file_size = url_size(url, headers=tmp_headers)

    if os.path.exists(filepath):
        if not force and file_size == os.path.getsize(filepath):
            if not is_part:
                if bar:
                    bar.done()
                print(
                    'Skipping {}: file already exists'.format(
                        tr(os.path.basename(filepath))
                    )
                )
            else:
                if bar:
                    bar.update_received(file_size)
            return
        else:
            if not is_part:
                if bar:
                    bar.done()
                print('Overwriting %s' % tr(os.path.basename(filepath)), '...')
    elif not os.path.exists(os.path.dirname(filepath)):
        os.mkdir(os.path.dirname(filepath))

    temp_filepath = filepath + '.download' if file_size != float('inf') \
        else filepath
    received = 0
    if not force:
        open_mode = 'ab'

        if os.path.exists(temp_filepath):
            received += os.path.getsize(temp_filepath)
            if bar:
                bar.update_received(os.path.getsize(temp_filepath))
    else:
        open_mode = 'wb'

    if received < file_size:
        if received:
            tmp_headers['Range'] = 'bytes=' + str(received) + '-'
        if refer:
            tmp_headers['Referer'] = refer
        kwargs = {
            'headers': tmp_headers,
        }
        if timeout:
            kwargs['timeout'] = timeout
        response = urlopen_with_retry(url, **kwargs)
        try:
            range_start = int(
                response.headers[
                    'content-range'
                ][6:].split('/')[0].split('-')[0]
            )
            end_length = int(
                response.headers['content-range'][6:].split('/')[1]
            )
            range_length = end_length - range_start
        except Exception:
            content_length = response.headers['content-length']
            range_length = int(content_length) if content_length \
                else float('inf')

        if file_size != received + range_length:
            received = 0
            if bar:
                bar.received = 0
            open_mode = 'wb'

        with open(temp_filepath, open_mode) as output:
            for chunk in response.iter_content(chunk_size=2048):
                if chunk:
                    output.write(chunk)
                    received += len(chunk)
                    if bar:
                        bar.update_received(len(chunk))

    assert received == os.path.getsize(temp_filepath), '{} == {} == {}'.format(
        received, os.path.getsize(temp_filepath), temp_filepath
    )

    if os.access(filepath, os.W_OK):
        # on Windows rename could fail if destination filepath exists
        os.remove(filepath)
    os.rename(temp_filepath, filepath)


class SimpleProgressBar:
    term_size = term.get_terminal_size()[1]

    def __init__(self, total_size, total_pieces=1):
        self.displayed = False
        self.total_size = total_size
        self.total_pieces = total_pieces
        self.current_piece = 1
        self.received = 0
        self.speed = ''
        self.last_updated = time.time()

        total_pieces_len = len(str(total_pieces))
        # 38 is the size of all statically known size in self.bar
        total_str = '%5s' % round(self.total_size / 1048576, 1)
        total_str_width = max(len(total_str), 5)
        self.bar_size = self.term_size - 28 - 2 * total_pieces_len \
            - 2 * total_str_width
        self.bar = '{:>4}%% ({:>%s}/%sMB) ├{:─<%s}┤[{:>%s}/{:>%s}] {}' % (
            total_str_width, total_str, self.bar_size, total_pieces_len,
            total_pieces_len
        )

    def update(self):
        self.displayed = True
        bar_size = self.bar_size
        percent = round(self.received * 100 / self.total_size, 1)
        if percent >= 100:
            percent = 100
        dots = bar_size * int(percent) // 100
        plus = int(percent) - dots // bar_size * 100
        if plus > 0.8:
            plus = '█'
        elif plus > 0.4:
            plus = '>'
        else:
            plus = ''
        bar = '█' * dots + plus
        bar = self.bar.format(
            percent, round(self.received / 1048576, 1), bar,
            self.current_piece, self.total_pieces, self.speed
        )
        sys.stdout.write('\r' + bar)
        sys.stdout.flush()

    def update_received(self, n):
        self.received += n
        time_diff = time.time() - self.last_updated
        bytes_ps = n / time_diff if time_diff else 0
        if bytes_ps >= 1024 ** 3:
            self.speed = '{:4.0f} GB/s'.format(bytes_ps / 1024 ** 3)
        elif bytes_ps >= 1024 ** 2:
            self.speed = '{:4.0f} MB/s'.format(bytes_ps / 1024 ** 2)
        elif bytes_ps >= 1024:
            self.speed = '{:4.0f} kB/s'.format(bytes_ps / 1024)
        else:
            self.speed = '{:4.0f}  B/s'.format(bytes_ps)
        self.last_updated = time.time()
        self.update()

    def update_piece(self, n):
        self.current_piece = n

    def done(self):
        if self.displayed:
            print()
            self.displayed = False


class PiecesProgressBar:
    def __init__(self, total_size, total_pieces=1):
        self.displayed = False
        self.total_size = total_size
        self.total_pieces = total_pieces
        self.current_piece = 1
        self.received = 0

    def update(self):
        self.displayed = True
        bar = '{0:>5}%[{1:<40}] {2}/{3}'.format(
            '', '=' * 40, self.current_piece, self.total_pieces
        )
        sys.stdout.write('\r' + bar)
        sys.stdout.flush()

    def update_received(self, n):
        self.received += n
        self.update()

    def update_piece(self, n):
        self.current_piece = n

    def done(self):
        if self.displayed:
            print()
            self.displayed = False


class DummyProgressBar:
    def __init__(self, *args):
        pass

    def update_received(self, n):
        pass

    def update_piece(self, n):
        pass

    def done(self):
        pass


def get_output_filename(urls, title, ext, output_dir, merge):
    # lame hack for the --output-filename option
    global output_filename
    if output_filename:
        if ext:
            return output_filename + '.' + ext
        return output_filename

    merged_ext = ext
    if (len(urls) > 1) and merge:
        from .processor.ffmpeg import has_ffmpeg_installed
        if ext in ['flv', 'f4v']:
            if has_ffmpeg_installed():
                merged_ext = 'mp4'
            else:
                merged_ext = 'flv'
        elif ext == 'mp4':
            merged_ext = 'mp4'
        elif ext == 'ts':
            if has_ffmpeg_installed():
                merged_ext = 'mkv'
            else:
                merged_ext = 'ts'
    return '{}.{}'.format(title, merged_ext)


def download_urls(
    urls, title, ext, total_size, output_dir='.', refer=None, merge=True,
    headers={}, thread=0, **kwargs
):
    assert urls
    if json_output:
        json_output_.download_urls(
            urls=urls, title=title, ext=ext, total_size=total_size,
            refer=refer
        )
        return
    if dry_run:
        print('Real URLs:\n{}'.format('\n'.join(urls)))
        return

    if player:
        launch_player(player, urls, refer=refer)
        return

    if not total_size:
        try:
            total_size = urls_size(urls, headers=headers)
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stdout)
            pass

    title = tr(get_filename(title))
    output_file = get_output_filename(urls, title, ext, output_dir, merge)
    output_filepath = os.path.join(output_dir, output_file)

    if total_size:
        if not force and os.path.exists(output_filepath) \
                and os.path.getsize(output_filepath) >= total_size * 0.9:
            print('Skipping {}: file already exists'.format(output_filepath))
            print()
            return
        bar = SimpleProgressBar(total_size, len(urls))
    else:
        bar = PiecesProgressBar(total_size, len(urls))

    if len(urls) == 1:
        url = urls[0]
        print('Downloading {} ...'.format(tr(output_file)))
        bar.update()
        url_save(
            url, output_filepath, bar, refer=refer, headers=headers, **kwargs
        )
        bar.done()
    else:
        print('Downloading {}.{} ...'.format(tr(title), ext))
        parts = [''] * len(urls)
        bar.update()
        piece = 0

        def _download(url):
            # Closure
            nonlocal piece
            index = urls.index(url)
            filename = '{}[{:0>2d}].{}'.format(title, index, ext)
            filepath = os.path.join(output_dir, filename)
            parts[index] = filepath  # 防止多线程环境下文件顺序会乱
            piece += 1
            bar.update_piece(piece)
            url_save(
                url, filepath, bar, refer=refer, is_part=True, headers=headers,
                **kwargs
            )
        if thread:
            with Pool(processes=thread) as pool:
                pool.map(_download, urls)
        else:
            for url in urls:
                _download(url)
        bar.done()

        if not merge:
            print()
            return

        if 'av' in kwargs and kwargs['av']:
            from .processor.ffmpeg import has_ffmpeg_installed
            if has_ffmpeg_installed():
                from .processor.ffmpeg import ffmpeg_concat_av
                ret = ffmpeg_concat_av(parts, output_filepath, ext)
                print('Merged into {}'.format(output_file))
                if ret == 0:
                    for part in parts:
                        os.remove(part)

        elif ext in ['flv', 'f4v']:
            try:
                from .processor.ffmpeg import has_ffmpeg_installed
                if has_ffmpeg_installed():
                    from .processor.ffmpeg import ffmpeg_concat_flv_to_mp4
                    ffmpeg_concat_flv_to_mp4(parts, output_filepath)
                else:
                    from .processor.join_flv import concat_flv
                    concat_flv(parts, output_filepath)
                print('Merged into {}'.format(output_file))
            except Exception:
                raise
            else:
                for part in parts:
                    os.remove(part)

        elif ext == 'mp4':
            try:
                from .processor.ffmpeg import has_ffmpeg_installed
                if has_ffmpeg_installed():
                    from .processor.ffmpeg import ffmpeg_concat_mp4_to_mp4
                    ffmpeg_concat_mp4_to_mp4(parts, output_filepath)
                else:
                    from .processor.join_mp4 import concat_mp4
                    concat_mp4(parts, output_filepath)
                print('Merged into {}'.format(output_file))
            except Exception:
                raise
            else:
                for part in parts:
                    os.remove(part)

        elif ext == 'ts':
            try:
                from .processor.ffmpeg import has_ffmpeg_installed
                if has_ffmpeg_installed():
                    from .processor.ffmpeg import ffmpeg_concat_ts_to_mkv
                    ffmpeg_concat_ts_to_mkv(parts, output_filepath)
                else:
                    from .processor.join_ts import concat_ts
                    concat_ts(parts, output_filepath)
                print('Merged into {}'.format(output_file))
            except Exception:
                raise
            else:
                for part in parts:
                    os.remove(part)

        else:
            print("Can't merge {} files".format(ext))

    print()


def download_rtmp_url(
    url, title, ext, params={}, total_size=0, output_dir='.', refer=None,
    merge=True
):
    assert url
    if dry_run:
        print('Real URL:\n%s\n' % [url])
        if params.get('-y', False):  # None or unset -> False
            print('Real Playpath:\n%s\n' % [params.get('-y')])
        return

    if player:
        from .processor.rtmpdump import play_rtmpdump_stream
        play_rtmpdump_stream(player, url, params)
        return

    from .processor.rtmpdump import (
        has_rtmpdump_installed, download_rtmpdump_stream
    )
    assert has_rtmpdump_installed(), 'RTMPDump not installed.'
    download_rtmpdump_stream(url, title, ext, params, output_dir)


def download_url_ffmpeg(
    url, title, ext, params={}, total_size=0, output_dir='.', refer=None,
    merge=True, stream=True, **kwargs
):
    assert url
    if dry_run:
        print('Real URL:\n%s\n' % [url])
        if params.get('-y', False):  # None or unset ->False
            print('Real Playpath:\n%s\n' % [params.get('-y')])
        return

    if player:
        launch_player(player, [url], refer=refer)
        return

    from .processor.ffmpeg import has_ffmpeg_installed, ffmpeg_download_stream
    assert has_ffmpeg_installed(), 'FFmpeg not installed.'

    global output_filename
    if output_filename:
        dotPos = output_filename.rfind('.')
        if dotPos > 0:
            title = output_filename[:dotPos]
            ext = output_filename[dotPos+1:]
        else:
            title = output_filename

    title = tr(get_filename(title))

    ffmpeg_download_stream(
        url, title, ext, params, output_dir, stream=stream, **kwargs
    )


def playlist_not_supported(name):
    def f(*args, **kwargs):
        raise NotImplementedError('Playlist is not supported for ' + name)
    return f


def print_info(site_info, title, type, size, **kwargs):
    if json_output:
        json_output_.print_info(
            site_info=site_info, title=title, type=type, size=size
        )
        return
    if type:
        type = type.lower()
    if type in ['3gp']:
        type = 'video/3gpp'
    elif type in ['asf', 'wmv']:
        type = 'video/x-ms-asf'
    elif type in ['flv', 'f4v']:
        type = 'video/x-flv'
    elif type in ['mkv']:
        type = 'video/x-matroska'
    elif type in ['mp3']:
        type = 'audio/mpeg'
    elif type in ['mp4']:
        type = 'video/mp4'
    elif type in ['mov']:
        type = 'video/quicktime'
    elif type in ['ts']:
        type = 'video/MP2T'
    elif type in ['webm']:
        type = 'video/webm'

    elif type in ['jpg']:
        type = 'image/jpeg'
    elif type in ['png']:
        type = 'image/png'
    elif type in ['gif']:
        type = 'image/gif'

    if type in ['video/3gpp']:
        type_info = '3GPP multimedia file (%s)' % type
    elif type in ['video/x-flv', 'video/f4v']:
        type_info = 'Flash video (%s)' % type
    elif type in ['video/mp4', 'video/x-m4v']:
        type_info = 'MPEG-4 video (%s)' % type
    elif type in ['video/MP2T']:
        type_info = 'MPEG-2 transport stream (%s)' % type
    elif type in ['video/webm']:
        type_info = 'WebM video (%s)' % type
    # elif type in ['video/ogg']:
    #    type_info = 'Ogg video (%s)' % type
    elif type in ['video/quicktime']:
        type_info = 'QuickTime video (%s)' % type
    elif type in ['video/x-matroska']:
        type_info = 'Matroska video (%s)' % type
    # elif type in ['video/x-ms-wmv']:
    #    type_info = 'Windows Media video (%s)' % type
    elif type in ['video/x-ms-asf']:
        type_info = 'Advanced Systems Format (%s)' % type
    # elif type in ['video/mpeg']:
    #    type_info = 'MPEG video (%s)' % type
    elif type in ['audio/mp4', 'audio/m4a']:
        type_info = 'MPEG-4 audio (%s)' % type
    elif type in ['audio/mpeg']:
        type_info = 'MP3 (%s)' % type
    elif type in ['audio/wav', 'audio/wave', 'audio/x-wav']:
        type_info = 'Waveform Audio File Format ({})'.format(type)

    elif type in ['image/jpeg']:
        type_info = 'JPEG Image (%s)' % type
    elif type in ['image/png']:
        type_info = 'Portable Network Graphics (%s)' % type
    elif type in ['image/gif']:
        type_info = 'Graphics Interchange Format (%s)' % type
    elif type in ['m3u8']:
        if 'm3u8_type' in kwargs:
            if kwargs['m3u8_type'] == 'master':
                type_info = 'M3U8 Master {}'.format(type)
        else:
            type_info = 'M3U8 Playlist {}'.format(type)
    else:
        type_info = 'Unknown type (%s)' % type

    maybe_print('Site:      ', site_info)
    maybe_print('Title:     ', unescape(tr(title)))
    print('Type:      ', type_info)
    if type != 'm3u8':
        print(
            'Size:      ', round(size / 1048576, 2),
            'MiB (' + str(size) + ' Bytes)'
        )
    if type == 'm3u8' and 'm3u8_url' in kwargs:
        print('M3U8 Url:   {}'.format(kwargs['m3u8_url']))
    print()


def mime_to_container(mime):
    mapping = {
        'video/3gpp': '3gp',
        'video/mp4': 'mp4',
        'video/webm': 'webm',
        'video/x-flv': 'flv',
    }
    if mime in mapping:
        return mapping[mime]
    else:
        return mime.split('/')[1]


def parse_host(host):
    """Parses host name and port number from a string.
    """
    if re.match(r'^(\d+)$', host) is not None:
        return ('0.0.0.0', int(host))
    if re.match(r'^(\w+)://', host) is None:
        host = '//' + host
    o = parse.urlparse(host)
    hostname = o.hostname or '0.0.0.0'
    port = o.port or 0
    return (hostname, port)


def set_proxy(proxy):
    session.proxies.update({
        'http': '%s:%s' % proxy,
        'https': '%s:%s' % proxy,
    })


def unset_proxy():
    session.proxies = {}


def download_main(download, download_playlist, urls, playlist, **kwargs):
    for url in urls:
        if re.match(r'https?://', url) is None:
            url = 'http://' + url

        if playlist:
            download_playlist(url, **kwargs)
        else:
            download(url, **kwargs)


def load_cookies(cookiefile):
    global cookies
    try:
        cookies = cookiejar.MozillaCookieJar(cookiefile)
        cookies.load()
    except Exception:
        import sqlite3
        cookies = cookiejar.MozillaCookieJar()
        con = sqlite3.connect(cookiefile)
        cur = con.cursor()
        try:
            cur.execute("""SELECT host, path, isSecure, expiry, name, value
                        FROM moz_cookies""")
            for item in cur.fetchall():
                c = cookiejar.Cookie(
                    0, item[4], item[5], None, False, item[0],
                    item[0].startswith('.'), item[0].startswith('.'),
                    item[1], False, item[2], item[3], item[3] == '', None,
                    None, {},
                )
                cookies.set_cookie(c)
        except Exception:
            pass
        # TODO: Chromium Cookies
        # SELECT host_key, path, secure, expires_utc, name, encrypted_value
        # FROM cookies
        # http://n8henrie.com/2013/11/use-chromes-cookies-for-easier-downloading-with-python-requests/


def set_socks_proxy(proxy):
    try:
        import socks
        socks_proxy_addrs = proxy.split(':')
        socks.set_default_proxy(
            socks.SOCKS5,
            socks_proxy_addrs[0],
            int(socks_proxy_addrs[1])
        )
        socket.socket = socks.socksocket

        def getaddrinfo(*args):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (args[0], args[1]))
            ]
        socket.getaddrinfo = getaddrinfo
    except ImportError:
        log.w(
            'Error importing PySocks library, socks proxy ignored.'
            'In order to use use socks proxy, please install PySocks.'
        )


def script_main(download, download_playlist, **kwargs):
    logging.basicConfig(format='[%(levelname)s] %(message)s')

    def print_version():
        version = __version__
        log.i(
            'version {}, a tiny downloader that scrapes the web.'.format(
                version
            )
        )

    parser = argparse.ArgumentParser(
        prog='lulu',
        usage='lulu [OPTION]... URL...',
        description='A tiny downloader that scrapes the web',
        add_help=False,
    )
    parser.add_argument(
        '-V', '--version', action='store_true',
        help='Print version and exit'
    )
    parser.add_argument(
        '-h', '--help', action='store_true',
        help='Print this help message and exit'
    )

    dry_run_grp = parser.add_argument_group(
        'Dry-run options', '(no actual downloading)'
    )
    dry_run_grp = dry_run_grp.add_mutually_exclusive_group()
    dry_run_grp.add_argument(
        '-i', '--info', action='store_true', help='Print extracted information'
    )
    dry_run_grp.add_argument(
        '-u', '--url', action='store_true',
        help='Print extracted information with URLs'
    )
    dry_run_grp.add_argument(
        '--json', action='store_true',
        help='Print extracted URLs in JSON format'
    )

    download_grp = parser.add_argument_group('Download options')
    download_grp.add_argument(
        '-n', '--no-merge', action='store_true', default=False,
        help='Do not merge video parts'
    )
    download_grp.add_argument(
        '--no-caption', action='store_true',
        help='Do not download captions (subtitles, lyrics, danmaku, ...)'
    )
    download_grp.add_argument(
        '-f', '--force', action='store_true', default=False,
        help='Force overwriting existing files'
    )
    download_grp.add_argument(
        '-F', '--format', metavar='STREAM_ID',
        help='Set video format to STREAM_ID'
    )
    download_grp.add_argument(
        '-O', '--output-filename', metavar='FILE', help='Set output filename'
    )
    download_grp.add_argument(
        '-o', '--output-dir', metavar='DIR', default='.',
        help='Set output directory'
    )
    download_grp.add_argument(
        '-p', '--player', metavar='PLAYER',
        help='Stream extracted URL to a PLAYER'
    )
    download_grp.add_argument(
        '-c', '--cookies', metavar='COOKIES_FILE',
        help='Load cookies.txt or cookies.sqlite'
    )
    download_grp.add_argument(
        '-t', '--timeout', metavar='SECONDS', type=int, default=600,
        help='Set socket timeout'
    )
    download_grp.add_argument(
        '-d', '--debug', action='store_true',
        help='Show traceback and other debug info'
    )
    download_grp.add_argument(
        '-I', '--input-file', metavar='FILE', type=argparse.FileType('r'),
        help='Read non-playlist URLs from FILE'
    )
    download_grp.add_argument(
        '-P', '--password', help='Set video visit password to PASSWORD'
    )
    download_grp.add_argument(
        '-l', '--playlist', action='store_true',
        help='Prefer to download a playlist'
    )
    download_grp.add_argument(
        '-T', '--thread', type=int, default=0,
        help=(
            'Use multithreading to download (only works for multiple-parts '
            'video)'
        )
    )

    proxy_grp = parser.add_argument_group('Proxy options')
    proxy_grp = proxy_grp.add_mutually_exclusive_group()
    proxy_grp.add_argument(
        '-x', '--http-proxy', metavar='HOST:PORT',
        help='Use an HTTP proxy for downloading'
    )
    proxy_grp.add_argument(
        '-y', '--extractor-proxy', metavar='HOST:PORT',
        help='Use an HTTP proxy for extracting only'
    )
    proxy_grp.add_argument(
        '--no-proxy', action='store_true', help='Never use a proxy'
    )
    proxy_grp.add_argument(
        '-s', '--socks-proxy', metavar='HOST:PORT',
        help='Use an SOCKS5 proxy for downloading'
    )

    download_grp.add_argument('--stream', help=argparse.SUPPRESS)
    download_grp.add_argument('--itag', help=argparse.SUPPRESS)

    parser.add_argument('URL', nargs='*', help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.help:
        print_version()
        parser.print_help()
        sys.exit()
    if args.version:
        print_version()
        sys.exit()

    if args.debug:
        # Set level of root logger to DEBUG
        logging.getLogger().setLevel(logging.DEBUG)

    global force
    global dry_run
    global json_output
    global player
    global extractor_proxy
    global output_filename

    output_filename = args.output_filename
    extractor_proxy = args.extractor_proxy

    info_only = args.info
    if args.url:
        dry_run = True
    if args.json:
        json_output = True
        # to fix extractors not use VideoExtractor
        dry_run = True
        info_only = False

    if args.cookies:
        load_cookies(args.cookies)

    caption = True
    stream_id = args.format or args.stream or args.itag
    if args.no_caption:
        caption = False
    if args.player:
        player = args.player
        caption = False

    if args.no_proxy:
        unset_proxy()
    else:
        if args.http_proxy:
            set_proxy(parse_host(args.http_proxy))
    if args.socks_proxy:
        set_socks_proxy(args.socks_proxy)

    URLs = []
    if args.input_file:
        logging.debug('you are trying to load urls from %s', args.input_file)
        if args.playlist:
            log.e(
                "reading playlist from a file is unsupported "
                "and won't make your life easier"
            )
            sys.exit(2)
        URLs.extend(args.input_file.read().splitlines())
        args.input_file.close()
    URLs.extend(args.URL)

    if not URLs:
        parser.print_help()
        sys.exit()

    socket.setdefaulttimeout(args.timeout)

    try:
        extra = {}
        if extractor_proxy:
            extra['extractor_proxy'] = extractor_proxy
        if stream_id:
            extra['stream_id'] = stream_id
        download_main(
            download, download_playlist,
            URLs, args.playlist,
            output_dir=args.output_dir, merge=not args.no_merge,
            info_only=info_only, json_output=json_output, caption=caption,
            password=args.password, thread=args.thread,
            **extra
        )
    except KeyboardInterrupt:
        if args.debug:
            raise
        else:
            sys.exit(1)
    except UnicodeEncodeError:
        if args.debug:
            raise
        log.e(
            '[error] oops, the current environment does not seem to support '
            'Unicode.'
        )
        log.e('please set it to a UTF-8-aware locale first,')
        log.e(
            'so as to save the video (with some Unicode characters) correctly.'
        )
        log.e('you can do it like this:')
        log.e('    (Windows)    % chcp 65001 ')
        log.e('    (Linux)      $ LC_CTYPE=en_US.UTF-8')
        sys.exit(1)
    except Exception:
        if not args.debug:
            log.e('[error] oops, something went wrong.')
            log.e(
                'don\'t panic, c\'est la vie. please try the following steps:'
            )
            log.e('  (1) Rule out any network problem.')
            log.e('  (2) Make sure lulu is up-to-date.')
            log.e('  (3) Check if the issue is already known, on')
            log.e('        https://github.com/iawia002/Lulu/issues')
            log.e('  (4) Run the command with \'--debug\' option,')
            log.e('      and report this issue with the full output.')
        else:
            print_version()
            log.i(args)
            raise
        sys.exit(1)


def google_search(url):
    keywords = match1(url, r'https?://(.*)')
    url = 'https://www.google.com/search?tbm=vid&q=%s' % parse.quote(keywords)
    page = get_content(url)
    videos = re.findall(
        r'<a href="(https?://[^"]+)" onmousedown="[^"]+">([^<]+)<', page
    )
    vdurs = re.findall(r'<span class="vdur _dwc">([^<]+)<', page)
    durs = [match1(unescape(dur), r'(\d+:\d+)') for dur in vdurs]
    print('Google Videos search:')
    for v in zip(videos, durs):
        print('- video:  {} [{}]'.format(
            unescape(v[0][1]),
            v[1] if v[1] else '?'
        ))
        print('# lulu %s' % log.sprint(v[0][0], log.UNDERLINE))
        print()
    print('Best matched result:')
    return(videos[0][0])


def url_to_module(url):
    try:
        video_host = match1(url, r'https?://([^/]+)/')
        video_url = match1(url, r'https?://[^/]+(.*)')
        assert video_host and video_url
    except AssertionError:
        url = google_search(url)
        video_host = match1(url, r'https?://([^/]+)/')
        video_url = match1(url, r'https?://[^/]+(.*)')

    if video_host.endswith('.com.cn') or video_host.endswith('.ac.cn'):
        video_host = video_host[:-3]
    domain = match1(video_host, r'(\.[^.]+\.[^.]+)$') or video_host
    assert domain, 'unsupported url: ' + url

    k = match1(domain, r'([^.]+)')
    if k in SITES:
        return (
            import_module('.'.join(['lulu', 'extractors', SITES[k]])),
            url
        )
    else:
        import http.client
        video_host = match1(url, r'https?://([^/]+)/')  # .cn could be removed
        if url.startswith('https://'):
            conn = http.client.HTTPSConnection(video_host)
        else:
            conn = http.client.HTTPConnection(video_host)
        conn.request('HEAD', video_url, headers=FAKE_HEADERS)
        res = conn.getresponse()
        location = res.getheader('location')
        if location and location != url and not location.startswith('/'):
            return url_to_module(location)
        else:
            return import_module('lulu.extractors.universal'), url


def any_download(url, **kwargs):
    m, url = url_to_module(url)
    m.download(url, **kwargs)


def any_download_playlist(url, **kwargs):
    m, url = url_to_module(url)
    m.download_playlist(url, **kwargs)


def main(**kwargs):
    script_main(any_download, any_download_playlist, **kwargs)
