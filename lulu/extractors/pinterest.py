#!/usr/bin/env python

import json

from lulu.common import (
    match1,
    url_info,
    get_content,
)
from lulu.extractor import VideoExtractor


class Pinterest(VideoExtractor):
    # site name
    name = 'Pinterest pinterest.com'

    # ordered list of supported stream types / qualities on this site
    # order: high quality -> low quality
    stream_types = [
        {'id': 'original'},  # contains an 'id' or 'itag' field at minimum
        {'id': 'small'},
    ]

    def prepare(self, **kwargs):
        # scrape the html
        content = get_content(self.url)
        # extract title
        self.title = match1(
            content,
            r'<meta property="og:description" name="og:description" '
            r'content="([^"]+)"'
        )

        data = match1(
            content,
            r'<script type="application/json" id=\'initial-state\'>(.+)'
            r'</script>'
        )
        data = json.loads(data)
        keys = list(data['resources']['data']['PinPageResource'].keys())
        orig_img = data['resources']['data']['PinPageResource'][keys[0]][
            'data'
        ]['images']['orig']['url']
        twit_img = match1(
            content,
            r'<meta property="twitter:image:src" name="twitter:image:src" '
            r'content="([^"]+)"'
        )
        # construct available streams
        if orig_img:
            self.streams['original'] = {'url': orig_img}
        if twit_img:
            self.streams['small'] = {'url': twit_img}

    def extract(self, **kwargs):
        for i in self.streams:
            # for each available stream
            s = self.streams[i]
            # fill in 'container' field and 'size' field (optional)
            _, s['container'], s['size'] = url_info(s['url'])
            # 'src' field is a list of processed urls for direct downloading
            # usually derived from 'url'
            s['src'] = [s['url']]


site = Pinterest()
download = site.download_by_url
# TBD: implement download_playlist
