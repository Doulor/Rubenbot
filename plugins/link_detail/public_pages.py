"""按作品页自己的数据结构取值；没有对应实体时不回退到网页标题。"""

import re
from urllib.parse import urlsplit

from .public_common import clock, decode, detail, field, obj, plain, seq, stats, text


def _youtube(page, ident):
    player = obj(page.variable('ytInitialPlayerResponse'))
    video = obj(player.get('videoDetails'))
    if video.get('videoId') != ident or field(player, 'playabilityStatus', 'status') != 'OK':
        return None
    thumbs = seq(field(video, 'thumbnail', 'thumbnails'))
    return detail(video.get('title'), author=video.get('author'), duration=clock(video.get('lengthSeconds')),
                  description=video.get('shortDescription'), cover=field(thumbs[-1], 'url') if thumbs else '',
                  published=field(player, 'microformat', 'playerMicroformatRenderer', 'publishDate'),
                  stats=stats(video, 播放='viewCount'))


def _tiktok(page, ident):
    scope = field(page.json_id('__UNIVERSAL_DATA_FOR_REHYDRATION__'), '__DEFAULT_SCOPE__', 'webapp.video-detail')
    if obj(scope).get('statusCode', 0) != 0:
        return None
    item = obj(field(scope, 'itemInfo', 'itemStruct') or field(page.json_id('SIGI_STATE'), 'ItemModule', ident))
    if text(item.get('id')) != ident or item.get('privateItem'):
        return None
    video = obj(item.get('video'))
    return detail(item.get('desc'), author=field(item, 'author', 'nickname') or item.get('author'),
                  duration=clock(video.get('duration')), cover=video.get('cover'), published=item.get('createTime'),
                  stats=stats(obj(item.get('stats')), 播放='playCount', 点赞='diggCount', 评论='commentCount', 分享='shareCount'))


def _kuaishou(page, ident):
    for entry in obj(page.variable('INIT_STATE')).values():
        photo = obj(field(entry, 'photo'))
        photo_id = photo.get('photoId') or photo.get('id')
        if not photo or (photo_id and text(photo_id) != ident) or obj(entry).get('result', 1) != 1:
            continue
        covers = seq(photo.get('coverUrls'))
        return detail(photo.get('caption'), author=photo.get('userName'),
                      cover=field(covers[0], 'url') if covers else '', duration=clock(photo.get('duration'), 1000),
                      stats=stats(photo, 点赞='likeCount', 播放='viewCount'), published=photo.get('timestamp'))
    # 桌面页把 GraphQL 的短视频详情和用户分别存在 Apollo 归一化缓存里。
    cache = obj(field(page.variable('__APOLLO_STATE__'), 'defaultClient'))
    photo = obj(cache.get(f'VisionVideoPhoto:{ident}'))
    if text(photo.get('id')) == ident:
        for item in cache.values():
            if field(item, 'photo', '__ref') == f'VisionVideoPhoto:{ident}':
                author = obj(cache.get(text(field(item, 'author', '__ref'))))
                return detail(photo.get('caption'), author=author.get('name'), cover=photo.get('coverUrl'),
                              duration=clock(photo.get('duration'), 1000), published=photo.get('timestamp'),
                              stats=stats(photo, 点赞='likeCount', 播放='viewCount'))
    return None


def _instagram(page, ident):
    # Relay 的请求名时常改动；按 shortcode 锁定媒体实体，不拿推荐列表的第一项。
    for _, typ, chunks in page.scripts:
        if typ != 'application/json':
            continue
        stack = [decode(''.join(chunks))]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get('shortcode') == ident or node.get('code') == ident:
                    edges = seq(field(node, 'edge_media_to_caption', 'edges'))
                    caption = field(edges[0], 'node', 'text') if edges else field(node, 'caption', 'text')
                    images = seq(field(node, 'image_versions2', 'candidates'))
                    card = detail(caption, author=field(node, 'owner', 'username') or field(node, 'user', 'username'),
                                  cover=node.get('display_url') or (field(images[0], 'url') if images else ''),
                                  duration=clock(node.get('video_duration')), published=node.get('taken_at_timestamp') or node.get('taken_at'),
                                  stats=stats(node, 点赞='like_count', 评论='comment_count', 播放='video_view_count'))
                    if card:
                        return card
                stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
    # 匿名页面常只提供分享卡；只接受指向同一帖子的内容卡，并要求原帖说明而非站点口号。
    meta = page.meta
    canonical = urlsplit(meta.get('og:url', ''))
    if (canonical.hostname not in ('www.instagram.com', 'instagram.com', 'instagr.am') or
            not re.fullmatch(rf'/(?:p|reels?|tv)/{re.escape(ident)}/?', canonical.path)):
        return None
    description = text(meta.get('og:description'))
    caption = re.search(r'[:：]\s*["“「](.+)["”」]\s*[.!。]?$', description)
    if not caption or not meta.get('og:image'):
        return None
    return detail(caption[1], author=meta.get('instapp:owner_user_name'), cover=meta['og:image'],
                  published=meta.get('article:published_time'))


def _zhihu(page, kind, ident, url):
    entities = obj(field(page.json_id('js-initialData'), 'initialState', 'entities'))
    post = obj(field(entities, kind, ident))
    if not post or text(post.get('id', ident)) != ident:
        return None
    title = post.get('title')
    if kind == 'answers':
        question_id = urlsplit(url).path.split('/')[2]
        q = obj(post.get('question'))
        if q.get('id') is not None and text(q['id']) != question_id:
            return None
        title = field(entities, 'questions', question_id, 'title') or q.get('title')
    return detail(title, author=field(post, 'author', 'name'), cover=post.get('titleImage'),
                  description=plain(post.get('excerpt') or post.get('content') or post.get('detail')),
                  stats=stats(post, 赞同='voteupCount', 评论='commentCount', 回答='answerCount', 浏览='visitCount'),
                  published=post.get('createdTime') or post.get('created'))


def _xiaoheihe(page, kind, ident):
    # Nuxt 的 devalue 数组中，对象的值是槽号。只读作品与所需字段，不递归展开整个引用图。
    table = seq(page.json_id('__NUXT_DATA__'))

    def slot(row, key):
        n = obj(row).get(key)
        return table[n] if type(n) is int and 0 <= n < len(table) else None

    for row in table:
        if kind == 'game' and ident in (text(slot(row, 'appid')), text(slot(row, 'steam_appid'))):
            card = detail(slot(row, 'name') or slot(row, 'name_en'), description=plain(slot(row, 'about_the_game')),
                          stats=stats({'score': text(slot(row, 'score'))}, 评分='score'),
                          cover=slot(row, 'header_image'), published=slot(row, 'release_date'))
            if card:
                return card
        if kind == 'post' and text(slot(row, 'link_id')) == ident:
            user = slot(row, 'user')
            body = slot(row, 'text')
            blocks = decode(body)
            if isinstance(blocks, list):
                body = ' '.join(plain(obj(b).get('text')) for b in blocks)
            card = detail(slot(row, 'title'), author=slot(user, 'username') or slot(user, 'nickname'),
                          description=plain(body), cover=slot(row, 'img'), published=slot(row, 'create_at'))
            if card:
                return card
    return None


def _shipinhao(page):
    # 不换 token、不打需要登录态的 POST；只处理首屏已经公开给浏览器的作品。
    state = obj(page.variable('__INITIAL_STATE__') or page.json_id('__NEXT_DATA__'))
    data = obj(field(state, 'props', 'pageProps') or state.get('data') or state)
    feed = obj(data.get('feedInfo'))
    if feed and not field(feed, 'errMsg', 'type'):
        return detail(feed.get('description'), author=field(data, 'authorInfo', 'nickname'), cover=feed.get('coverUrl'),
                      published=feed.get('createtime'), duration=clock(feed.get('videoDuration') or field(feed, 'h264VideoInfo', 'duration')),
                      stats=stats(feed, 点赞='likeCountFmt', 评论='commentCountFmt', 转发='forwardCountFmt'))
    # 微信分享页也可直接暴露 VideoObject；不接受全站通用的 OG 标题和图标。
    for _, typ, chunks in page.scripts:
        if typ == 'application/ld+json':
            video = obj(decode(''.join(chunks)))
            if video.get('@type') == 'VideoObject' and video.get('thumbnailUrl') and video.get('uploadDate'):
                return detail(video.get('name'), cover=video.get('thumbnailUrl'), description=video.get('description'),
                              author=field(video, 'author', 'name'), published=video.get('uploadDate'))
    return None


def parse(platform, kind, ident, page, url):
    if platform == 'youtube':
        return _youtube(page, ident)
    if platform == 'tiktok':
        return _tiktok(page, ident)
    if platform == 'kuaishou':
        return _kuaishou(page, ident)
    if platform == 'instagram':
        return _instagram(page, ident)
    if platform == 'zhihu':
        return _zhihu(page, kind, ident, url)
    if platform == 'xiaoheihe':
        return _xiaoheihe(page, kind, ident)
    if platform == 'shipinhao':
        return _shipinhao(page)
    if platform == 'acfun':
        video = obj(page.variable('videoInfo'))
        if text(video.get('dougaId', ident)) != ident:
            return None
        return detail(video.get('title'), author=field(video, 'user', 'name'), cover=video.get('coverUrl'),
                      description=video.get('description'), duration=clock(field(video, 'currentVideoInfo', 'durationMillis'), 1000),
                      stats=stats(video, 播放='viewCount', 点赞='likeCount'), published=video.get('createTime'))
    if platform == 'nga' and page.node('postcontent0'):
        return detail(page.node('postsubject0'), description=page.node('postcontent0'),
                      author=page.node('postauthor0'), published=page.node('postdate0'))
    return None
