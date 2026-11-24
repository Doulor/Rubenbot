"""公开作品详情：未知字段留空，展示时不补零、不猜测。"""

from dataclasses import dataclass, field


@dataclass
class Detail:
    title: str = ""
    author: str = ""
    cover: str = ""
    duration: str = ""
    stats: dict[str, str | int] = field(default_factory=dict)
    description: str = ""
    published: str = ""
    url: str = ""

    def render(self, platform: str) -> str | None:
        def clean(value, limit):
            return " ".join(str(value or "").split())[:limit]

        title = clean(self.title, 180)
        description = clean(self.description, 360)
        if description == title:
            description = ""
        lines = []
        for label, value in (("作者", self.author), ("时长", self.duration), ("发布", self.published)):
            if text := clean(value, 100):
                lines.append(f"{label}：{text}")
        stats = [f"{clean(k, 10)}：{clean(str(v), 30)}" for k, v in self.stats.items()
                 if v is not None and str(v).strip()][:6]
        if stats:
            lines.append(" · ".join(stats))
        if description:
            lines.append(description)
        if not title or not (lines or self.cover):
            return None
        return "\n".join([f"【{platform}】{title}", *lines, self.url])
