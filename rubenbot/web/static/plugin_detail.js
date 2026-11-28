/* 插件详情页：介绍 / 版本 / 作者 / 权限 / 触发方式 / 配置说明 / 启用状态。
 *
 * 单独成文件，不改 app.js 里的既有函数；app.js 的统一插件页（#/plugins/<id>/detail）
 * 把详情内容交给这里的 renderPluginDetailContent，左侧插件列表与「配置 | 详情」
 * 子 tab 都由 app.js 管，本文件只负责详情区本身。
 * 只读展示 + schema 驱动的配置表单；所有文本仍走 textContent。
 *
 * 本文件与 app.js 共用全局作用域：两边都只用顶层函数声明与 const 常量，
 * 不依赖彼此的加载顺序（index.html 先加载本文件，便于深链首帧就能渲染）。
 */
"use strict";

/* 与 app.js 的同名判断保持一致；单独一份是为了不依赖加载顺序。 */
const PLUGIN_DETAIL_STATE_LABEL = {
  not_installed: "未安装",
  installed: "已安装",
  enabled: "已启用",
};

/** 默认值 / 可选值在说明区的显示形式。 */
function detailValueText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "开" : "关";
  if (Array.isArray(value)) return value.length ? value.join("、") : "（空）";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** 只放行 http(s) 链接：manifest / 索引里的仓库地址不可信，别让 javascript: 进 href。 */
function detailHref(url) {
  const text = String(url || "").trim();
  return /^https?:\/\//i.test(text) ? text : "";
}

/** 详情页顶部状态徽标：启用状态 + 可更新 / 不合规等标记。 */
function pluginDetailTags(data) {
  const tags = [];
  const state = String(data.state || "");
  if (state === "enabled") tags.push(h("span", { class: "tag good" }, "已启用"));
  else if (state === "installed") tags.push(h("span", { class: "tag" }, "已安装"));
  else if (state === "not_installed") tags.push(h("span", { class: "tag outline" }, "未安装"));
  if (!data.valid) tags.push(h("span", { class: "tag bad" }, "manifest 不合规"));
  if (data.core_too_old) tags.push(h("span", { class: "tag bad" }, "核心版本过低"));
  if (data.updatable) tags.push(h("span", { class: "tag warn" }, "可更新"));
  if (data.loaded) tags.push(h("span", { class: "tag outline" }, "已加载"));
  return tags;
}

/** 权限清单：高危项标红；已装插件同时标出是否已授权。 */
function pluginDetailPermissions(permissions) {
  const rows = permissions.items || [];
  if (!rows.length) {
    return h("p", { class: "field-hint" }, "该插件没有声明任何权限。");
  }
  return h(
    "ul",
    { class: "perm-list" },
    ...rows.map((info) => {
      const granted = info.granted;
      const suffix =
        granted === undefined
          ? null
          : h(
              "span",
              { class: `tag ${granted ? "good" : "warn"}` },
              granted ? "已授权" : "未授权"
            );
      return h(
        "li",
        { class: info.high_risk ? "high-risk" : "" },
        h("strong", {}, info.label || info.id, info.high_risk ? h("span", { class: "tag bad" }, "高危") : null),
        h(
          "span",
          {},
          h("span", {}, info.description || ""),
          suffix ? [" ", suffix] : null
        )
      );
    })
  );
}

/** 触发方式：manifest 说明 + 运行时命令 / 钩子。 */
function pluginDetailTriggers(data) {
  const rows = [];
  if (data.triggers && data.triggers.length) {
    rows.push(h("p", { class: "field-hint", style: { margin: 0 } }, data.triggers.join("；")));
  }
  if (data.commands && data.commands.length) {
    rows.push(
      h(
        "div",
        { class: "detail-triggers" },
        ...data.commands.map((name) => h("span", { class: "chip" }, `/${name}`))
      )
    );
  }
  if (data.hooks && data.hooks.length) {
    rows.push(
      h(
        "div",
        { class: "detail-triggers" },
        ...data.hooks.map((hook) =>
          h(
            "span",
            { class: "tag outline", title: hook.description || hook.name },
            `${hook.name}${hook.registered === null ? "" : ` · ${hook.registered}`}`
          )
        )
      )
    );
  }
  return rows.length
    ? h("div", { style: { display: "grid", gap: "8px" } }, ...rows)
    : h("p", { class: "field-hint" }, "该插件没有声明触发方式（以 plugin.py 实际注册为准）。");
}

/** 配置说明：字段、类型、默认值、范围 / 可选值与用途。 */
function pluginDetailSchemaDocs(docs) {
  if (!docs || !docs.length) {
    return h("p", { class: "field-hint" }, "该插件没有声明配置项。");
  }
  return h(
    "ul",
    { class: "perm-list schema-docs" },
    ...docs.map((doc) => {
      const meta = [
        doc.type_label,
        doc.range ? `范围 ${doc.range}` : null,
        doc.options && doc.options.length
          ? `可选：${doc.options.map((option) => option.label || option.value).join(" / ")}`
          : null,
      ].filter(Boolean);
      return h(
        "li",
        {},
        h("strong", {}, doc.label || doc.key),
        h(
          "span",
          {},
          h("span", {}, doc.description || "（没有写说明）"),
          h("span", { class: "field-hint" }, `${meta.join(" · ")} · 默认 ${doc.secret ? "已设置" : detailValueText(doc.default)}`)
        )
      );
    })
  );
}

/** 详情页主体：标题、元信息、介绍、触发、权限与配置说明。配置表单不在这里，改配置去上方的「配置」子 tab。 */
function pluginDetailBody(data) {
  const idOf = encodeURIComponent(data.id || data.plugin_name || "");
  const chips = pluginDetailTags(data);
  const meta = [
    ["版本", data.version ? `v${data.version}` : "—"],
    ["作者", data.author || "—"],
    ["分类", data.category || "工具"],
    ["状态", data.status_text || PLUGIN_DETAIL_STATE_LABEL[data.state] || data.state || "—"],
    [
      "运行",
      data.loaded ? "已加载" : data.enabled ? "已启用，等待加载" : "未加载",
    ],
  ];
  if (data.latest_version && data.latest_version !== data.version) {
    meta.push(["最新版本", `v${data.latest_version}`]);
  }
  if (data.local_path) meta.push(["本地目录", data.local_path]);

  const intro = card("介绍", {
    body: data.readme && data.readme.text
      ? h(
          "div",
          { class: "plugin-readme-wrap" },
          h("pre", { class: "plugin-readme" }, data.readme.text),
          data.readme.truncated ? h("p", { class: "field-hint" }, `原文超过 128 KiB，只展示前 128 KiB（${data.readme.name}）。`) : null
        )
      : h(
          "div",
          { style: { display: "grid", gap: "6px" } },
          h("p", { class: "soft", style: { margin: 0 } }, data.description || "这个插件没有写说明。"),
          h("p", { class: "field-hint" }, "插件目录里没有 README，以上是 manifest 的一句话说明。")
        ),
  });

  const repository = detailHref(data.repository);
  const head = h(
    "div",
    { class: "plugin-detail-head" },
    h("div", { class: "market-icon", "aria-hidden": "true" }, String(data.icon || "").trim() || avatarText(data.name)),
    h(
      "div",
      { class: "plugin-detail-heading" },
      h("h2", { class: "plugin-detail-name" }, data.name || data.id),
      h(
        "div",
        { class: "row-sub" },
        [data.author || "未知作者", data.version ? `v${data.version}` : "", data.category || "工具"]
          .filter(Boolean)
          .join(" · ")
      ),
      h("div", { class: "market-tags" }, ...chips)
    ),
    repository
      ? h(
          "a",
          { class: "btn btn-sm btn-ghost", href: repository, target: "_blank", rel: "noopener noreferrer" },
          icon("about"),
          "仓库"
        )
      : null
  );

  const overview = card("", {
    body: h(
      "div",
      { class: "plugin-detail-body" },
      head,
      h(
        "p",
        { class: "soft", style: { margin: 0 } },
        data.description || "这个插件没有写说明。"
      ),
      data.invalid_reason
        ? h("div", { class: "notice bad" }, icon("warning"), h("span", {}, `不合规：${data.invalid_reason}`))
        : null,
      h(
        "div",
        { class: "market-kv-grid" },
        ...meta.map(([label, value]) =>
          h("div", { class: "market-kv" }, h("span", { class: "field-label" }, label), h("span", {}, detailValueText(value)))
        )
      )
    ),
  });

  const triggers = card("触发方式", { body: pluginDetailTriggers(data) });
  const permissions = card(
    "权限清单",
    {
      body: h(
        "div",
        { style: { display: "grid", gap: "8px" } },
        data.permissions && data.permissions.declared && data.permissions.declared.length
          ? h(
              "p",
              { class: "field-hint", style: { margin: 0 } },
              data.installed
                ? `共声明 ${data.permissions.declared.length} 项，已授权 ${(data.permissions.granted || []).length} 项。更新不会补授新权限；确认新声明后，可停用再启用。`
                : `共声明 ${data.permissions.declared.length} 项；安装不会授权，显式启用即授予当前声明的权限。`
            )
          : null,
        pluginDetailPermissions(data.permissions || {}),
      ),
    }
  );
  const docs = card("配置说明", { body: pluginDetailSchemaDocs(data.schema_docs) });

  // 详情页只做详情：介绍、触发、权限、配置说明，外加插件自己的动态管理区
  //（比如 RSS 的订阅列表）。配置表单不在这里放，改配置去上方的「配置」子 tab。
  return [overview, h("div", { class: "grid grid-2" }, intro, triggers), h("div", { class: "grid grid-2" }, permissions, docs)];
}

/** RSS 插件专属：按订阅源分组的订阅管理（新增表单 + 提醒前缀 + 删除）。 */
async function renderRssSubscriptions(editor) {
  const mount = h("div", {}, skeleton(2));
  editor.appendChild(mount);

  /** 新增订阅表单：地址 + 目标类型 + 目标号码 + 可选提醒前缀。 */
  function buildAddForm() {
    const draft = { feed_url: "", target_type: "group", target_id: "", prefix: "" };
    const urlInput = textField("", (value) => { draft.feed_url = value; }, { placeholder: "https://example.com/feed.xml", mono: true });
    const typeSelect = selectField("group", [
      { value: "group", label: "群" },
      { value: "private", label: "私聊" },
    ], (value) => { draft.target_type = value; });
    const idInput = textField("", (value) => { draft.target_id = value; }, { placeholder: "群号或 QQ 号" });
    const prefixInput = textField("", (value) => { draft.prefix = value; }, { placeholder: "{rss}发新帖子了" });
    const submit = h(
      "button",
      {
        class: "btn",
        type: "button",
        onclick: async (ev) => {
          const btn = ev.currentTarget;
          const feed_url = draft.feed_url.trim();
          const target_id = draft.target_id.trim();
          if (!feed_url || !target_id) {
            toast("订阅地址和目标号码都要填", "error");
            return;
          }
          btn.disabled = true;
          try {
            await api.post("/api/plugins/rss/subscriptions", {
              feed_url,
              target_type: draft.target_type,
              target_id,
              prefix: draft.prefix,
            });
            toast("订阅已添加");
            await load();
          } catch (err) {
            if (err instanceof Unauthorized) return;
            toast(`添加失败：${err.message}`, "error");
            btn.disabled = false;
          }
        },
      },
      "添加订阅"
    );
    return card("新增订阅", {
      body: h(
        "div",
        { style: { display: "grid", gap: "10px" } },
        h("div", { class: "field" }, h("span", { class: "field-label" }, "订阅地址"), urlInput),
        h(
          "div",
          { style: { display: "grid", gridTemplateColumns: "1fr 2fr", gap: "10px" } },
          h("div", { class: "field" }, h("span", { class: "field-label" }, "目标类型"), typeSelect),
          h("div", { class: "field" }, h("span", { class: "field-label" }, "目标号码"), idInput)
        ),
        h(
          "div",
          { class: "field" },
          h("span", { class: "field-label" }, "提醒前缀（可选）"),
          prefixInput,
          h("span", { class: "field-hint" }, "{rss}=网站名称、{count}=本轮新文章数、{url}=订阅地址；留空不发提醒")
        ),
        h("div", {}, submit)
      ),
    });
  }

  /** 一条订阅行：目标 + 上次抓取 + 失败警告 + 提醒前缀编辑 + 删除。 */
  function buildSubRow(sub) {
    const target = sub.target_type === "group" ? `群 ${sub.target_id}` : `私聊 ${sub.target_id}`;
    const failed = (sub.fail_count || 0) >= 5;
    const prefixInput = h("input", {
      class: "input",
      type: "text",
      style: { flex: "1", minWidth: 0 },
      value: sub.prefix || "",
      placeholder: "{rss}发新帖子了",
      onkeydown: (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          savePrefix();
        }
      },
    });
    const savePrefix = async () => {
      saveBtn.disabled = true;
      try {
        await api.patch("/api/plugins/rss/subscriptions", {
          feed_url: sub.feed_url,
          target_type: sub.target_type,
          target_id: String(sub.target_id),
          prefix: prefixInput.value,
        });
        toast("提醒前缀已保存");
        await load();
      } catch (err) {
        if (err instanceof Unauthorized) return;
        toast(`保存失败：${err.message}`, "error");
        saveBtn.disabled = false;
      }
    };
    const saveBtn = h(
      "button",
      { class: "btn btn-sm", type: "button", onclick: savePrefix },
      "保存"
    );
    const deleteBtn = h(
      "button",
      {
        class: "btn btn-sm btn-danger",
        type: "button",
        onclick: async (ev) => {
          const btn = ev.currentTarget;
          btn.disabled = true;
          try {
            const qs = new URLSearchParams({
              feed_url: sub.feed_url,
              target_type: sub.target_type,
              target_id: String(sub.target_id),
            });
            await api.del(`/api/plugins/rss/subscriptions?${qs}`);
            await load();
          } catch (err) {
            if (err instanceof Unauthorized) return;
            toast(`删除失败：${err.message}`, "error");
            btn.disabled = false;
          }
        },
      },
      "删除"
    );
    return h(
      "div",
      { style: { display: "grid", gap: "6px", padding: "10px 0", borderTop: "1px solid var(--border)" } },
      h(
        "div",
        { style: { display: "flex", gap: "10px", alignItems: "center" } },
        h(
          "div",
          { style: { flex: "1", minWidth: 0 } },
          h("div", { style: { fontWeight: 600 } }, target),
          h(
            "div",
            { class: "field-hint" },
            `上次抓取 ${fmtRelative(sub.last_checked_at)}${failed ? " · 连续失败，已停推" : ""}`
          )
        ),
        deleteBtn
      ),
      h(
        "div",
        { style: { display: "flex", gap: "8px", alignItems: "center" } },
        prefixInput,
        saveBtn
      )
    );
  }

  /** 一个订阅源一张卡片：源名和地址只写一次，下面列出所有订阅者。 */
  function buildGroupCard(feedUrl, rows) {
    return card(rows[0].feed_title || feedUrl, {
      hint: `${rows.length} 个订阅`,
      body: h(
        "div",
        { style: { display: "grid" } },
        h(
          "div",
          { class: "field-hint", style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
          feedUrl
        ),
        ...rows.map((sub) => buildSubRow(sub))
      ),
    });
  }

  const load = async () => {
    let payload;
    try {
      payload = await api.get("/api/plugins/rss/subscriptions");
    } catch (err) {
      if (err instanceof Unauthorized) return;
      mount.replaceChildren(
        card("订阅管理", {
          body: h("p", { class: "field-hint" }, `订阅列表读不到：${err.message}`),
        })
      );
      return;
    }
    const items = payload.items || [];
    const groups = new Map();
    for (const sub of items) {
      if (!groups.has(sub.feed_url)) groups.set(sub.feed_url, []);
      groups.get(sub.feed_url).push(sub);
    }
    const children = [buildAddForm()];
    if (!items.length) {
      children.push(
        card("订阅列表", {
          body: h(
            "p",
            { class: "field-hint" },
            "还没有订阅。在上面填地址、目标和提醒前缀添加；也可以在私聊或群里发 /rss 订阅 <地址> 添加。"
          ),
        })
      );
    } else {
      for (const [feedUrl, rows] of groups) {
        children.push(buildGroupCard(feedUrl, rows));
      }
    }
    mount.replaceChildren(...children);
  };
  await load();
}

/** 详情子 tab 的内容：只取该插件的详情并渲染，列表与子 tab 由 app.js 的统一插件页管。
 *  不再自己拉市场索引取菜单——菜单是已安装列表，和配置 tab 共用一份。
 */
async function renderPluginDetailContent(editor, currentId) {
  try {
    const payload = await api.get(`/api/plugins/${encodeURIComponent(currentId)}/detail`);
    editor.replaceChildren(...pluginDetailBody(payload.plugin || {}));
    if (currentId === "rss") await renderRssSubscriptions(editor);
  } catch (err) {
    if (err instanceof Unauthorized) return false;
    editor.replaceChildren(
      card("", {
        body: [
          emptyState("warning", "读取详情失败", err.message),
          h("button", { class: "btn", type: "button", onclick: () => renderRoute() }, "重试"),
        ],
      })
    );
    return false;
  }
}
