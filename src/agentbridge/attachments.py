"""Bounded inline inputs shared by native provider transports."""
import base64
import binascii
import hashlib

from .errors import BridgeError, UnsupportedError

MAX_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}


def normalize(values):
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)) or len(values) > 8:
        raise BridgeError('invalid_attachment', 'Supply at most eight inline attachments.')
    result, total = [], 0
    for value in values:
        if not isinstance(value, dict):
            raise BridgeError('invalid_attachment', 'Each attachment must be an object.')
        kind = value.get('type')
        if not isinstance(kind, str) or kind not in {'image', 'text'}:
            raise UnsupportedError('Attachments support inline images and UTF-8 text.')
        allowed = {'type', 'name', 'data', 'media_type'} if kind == 'image' else {'type', 'name', 'text'}
        if set(value) - allowed:
            raise BridgeError('invalid_attachment', 'Attachment fields are not recognized; paths and URLs are not accepted.')
        name = value.get('name', 'attachment')
        if not isinstance(name, str) or not name or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise BridgeError('invalid_attachment', 'Use a short, printable attachment name.')
        if kind == 'image':
            encoded, media = value.get('data'), value.get('media_type')
            if not isinstance(media, str) or media not in IMAGE_TYPES or not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 4:
                raise BridgeError('invalid_attachment', 'Use a bounded base64 PNG, JPEG, WebP or GIF image.')
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise BridgeError('invalid_attachment', 'The image is not valid base64.') from None
            valid = ((media == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n'))
                     or (media == 'image/jpeg' and raw.startswith(b'\xff\xd8\xff'))
                     or (media == 'image/gif' and raw[:6] in {b'GIF87a', b'GIF89a'})
                     or (media == 'image/webp' and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP'))
            if not valid:
                raise BridgeError('invalid_attachment', 'The image header does not match its media type.')
            item = {'type': kind, 'name': name, 'media_type': media,
                    'data': base64.b64encode(raw).decode('ascii')}
        else:
            text = value.get('text')
            if not isinstance(text, str) or not text or len(text) > MAX_BYTES:
                raise BridgeError('invalid_attachment', 'Text attachments require bounded, nonempty UTF-8 text.')
            try:
                raw = text.encode('utf-8')
            except UnicodeEncodeError:
                raise BridgeError('invalid_attachment', 'The attachment must contain valid UTF-8 text.') from None
            item = {'type': kind, 'name': name, 'text': text}
        total += len(raw)
        if total > MAX_BYTES:
            raise BridgeError('invalid_attachment', 'Attachments exceed the combined five MiB limit.')
        result.append(item)
    return tuple(result)


def text_prompt(prompt, attachments):
    return prompt + ''.join('\n\nAttached text (' + item['name'] + '):\n' + item['text']
                            for item in attachments if item['type'] == 'text')


def images(attachments):
    return [item for item in attachments if item['type'] == 'image']


def descriptors(attachments):
    return [{'type': item['type'], 'name': item['name'], 'media_type': item.get('media_type', 'text/plain'),
             'sha256': hashlib.sha256(base64.b64decode(item['data']) if item['type'] == 'image'
                                      else item['text'].encode()).hexdigest()}
            for item in attachments]
