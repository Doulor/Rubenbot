"""第一方匿名 GET 接口；核对作品标识后才使用数据。"""

import re
from urllib.parse import quote, urlsplit

from .public_common import Page, clock, detail, field, obj, plain, seq, stats, text


def _iwara(kind, ident, data):
    if (data.get('id') != ident or data.get('rating') != 'general' or data.get('private')
            or data.get('unlisted') or data.get('message')):
        return None
    file_id = text(field(data, 'file', 'id'))
    frame = text(data.get('thumbnail'))
    cover = ''
    if kind == 'video' and re.fullmatch(r'[\w-]+', file_id, re.ASCII) and re.fullmatch(r'\d{1,3}', frame):
        cover = f'https://i.iwara.tv/image/thumbnail/{file_id}/thumbnail-{frame.zfill(2)}.jpg'
    elif kind == 'image':
        image = obj(next(iter(seq(data.get('files'))), None))
        name, image_id = text(image.get('name')), text(image.get('id'))
        if re.fullmatch(r'[\w-]+', image_id, re.ASCII) and name and '/' not in name and name not in ('.', '..'):
            cover = f'https://i.iwara.tv/image/original/{image_id}/{quote(name)}'
    return detail(data.get('title'), author=field(data, 'user', 'name'), cover=cover,
                  description=plain(data.get('body')), duration=clock(field(data, 'file', 'duration')),
                  stats=stats(data, 播放='numViews', 点赞='numLikes', 评论='numComments'),
                  published=data.get('createdAt') or data.get('updatedAt'))


def _song(ident, data):
    if data.get('code') != 200:
        return None
    for song in seq(data.get('songs')):
        song = obj(song)
        if text(song.get('id')) == ident:
            album = obj(song.get('album'))
            return detail(song.get('name'), author=' / '.join(text(obj(a).get('name')) for a in
                          seq(song.get('artists')) if text(obj(a).get('name'))),
                          cover=album.get('picUrl'), duration=clock(song.get('duration'), 1000),
                          description=album.get('name'), published=album.get('publishTime'))
    return None


def _weibo(ident, data):
    post = obj(data.get('data'))
    if data.get('ok') != 1 or ident not in {text(post.get(k)) for k in ('id', 'idstr', 'bid', 'mid')}:
        return None
    body = plain(field(post, 'longText', 'longTextContent') or post.get('text'))
    page = obj(post.get('page_info'))
    picture = obj(next(iter(seq(post.get('pics'))), None))
    return detail(page.get('page_title') or body, author=field(post, 'user', 'screen_name'),
                  cover=field(page, 'page_pic', 'url') or field(picture, 'large', 'url') or picture.get('url'),
                  stats=stats(post, 点赞='attitudes_count', 评论='comments_count', 转发='reposts_count'),
                  description=body, published=post.get('created_at'))


def parse(platform, kind, ident, data):
    if platform == 'iwara':
        return _iwara(kind, ident, data)
    if platform == 'netease_music':
        return _song(ident, data)
    if platform == 'weibo':
        return _weibo(ident, data)
    if platform == 'x':
        # 官方 oEmbed 的正文只在 blockquote > p，外层还带脚本与署名，不拼进推文。
        target = urlsplit(text(data.get('url')))
        if (data.get('type') == 'rich' and target.hostname in ('x.com', 'twitter.com', 'www.twitter.com')
                and re.fullmatch(rf'/(?:\w+|i/web)/status/{ident}/?', target.path, re.ASCII)):
            return detail(Page(text(data.get('html'))).node('tweet_text'), author=data.get('author_name'))
    if platform == 'zhihu' and text(data.get('id')) == ident:
        body = plain(data.get('content_html') or data.get('contentHtml') or data.get('content'))
        return detail(body, author=field(data, 'author', 'name'), published=data.get('created_time'),
                      stats=stats(data, 点赞='like_count', 评论='comment_count'))
    return None
