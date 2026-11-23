/* 插件详情页：介绍 / 版本 / 作者 / 权限 / 触发方式 / 配置说明 / 启用状态。
 *
 * 单独成文件，不改 app.js 里的既有函数；app.js 的路由只把
 * #/plugins/detail/<id> 交给这里的 renderPluginDetail。
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

/** 详情页左侧菜单：已装与可装插件都列出来，当前选中的高亮。 */
function pluginDetailMenu(items, currentId) {
  const rows = items.length
    ? items.map((item) =>
        h(
          "a",
          {
            class: `settings-menu-item${item.id === currentId ? " active" : ""}`,
            href: `#/plugins/detail/${encodeURIComponent(item.id || item.plugin_name)}`,
            "aria-current": item.id === currentId ? "page" : null,
          },
          item.name || item.id
        )
      )
    : [h("span", { class: "field-hint" }, "还没有可查看的插件")];
  return h(
    "nav",
    { class: "settings-menu", "aria-label": "插件详情" },
    h("div", { class: "settings-menu-group" }, "插件"),
    ...rows
  );
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

/** 单个 schema 控件的包装：kind 决定事件与回读时怎么写值。 */
function schemaFieldControl(spec, value, onChange, secretSet) {
  const kind = String(spec.type || "string");
  const label = spec.label || spec.description || spec.key;
  const options = Array.isArray(spec.options) ? spec.options : [];
  if (kind === "bool") {
    return { kind: "switch", node: switchField(Boolean(value), onChange, { label }) };
  }
  if (kind === "int" || kind === "number") {
    return {
      kind: "input",
      node: numberField(value, (next) => onChange(next === "" ? "" : Number(next)), {
        step: spec.step === undefined || spec.step === null ? (kind === "int" ? 1 : "any") : spec.step,
        min: spec.min,
        max: spec.max,
      }),
    };
  }
  if (kind === "list") {
    // 标签组的增删状态在闭包里：保存后若服务端归一了列表（去空项、去重），
    // 需要整组重建才能和回读值一致
    const holder = h("div", {});
    let build = (values) =>
      listEditor(Array.isArray(values) ? values : [], onChange, {
        placeholder: spec.placeholder || `添加${label}`,
      });
    holder.append(build(value));
    const refresh = (next) => holder.replaceChildren(build(next));
    return { kind: "list", node: holder, refresh };
  }
  if (options.length) {
    const control = selectField(
      value,
      options.map((option) => ({ value: option.value, label: option.label || option.value })),
      onChange
    );
    return { kind: "select", node: control };
  }
  if (spec.secret) {
    const input = h("input", {
      class: "input",
      type: "password",
      autocomplete: "new-password",
      placeholder: (secretSet || {})[spec.key] ? "已设置，留空保留原值" : "尚未设置",
      oninput: (event) => onChange(event.target.value),
    });
    return { kind: "secret", node: input };
  }
  if (kind === "text") {
    const area = h("textarea", {
      class: "input",
      rows: 4,
      placeholder: spec.placeholder || "",
      value: value === undefined || value === null ? "" : String(value),
      oninput: (event) => onChange(event.target.value),
    });
    return { kind: "textarea", node: area };
  }
  return {
    kind: "input",
    node: textField(value, onChange, { placeholder: spec.placeholder || "" }),
  };
}

/** 把回读值写回已渲染的控件（保存后与服务端结果对齐）。 */
function applySchemaValue(entry, value) {
  const node = entry.node;
  if (entry.kind === "switch") {
    const next = Boolean(value);
    node.setAttribute("aria-checked", next ? "true" : "false");
    Motion.switchThumb(node.querySelector(".switch-thumb"), next);
  } else if (entry.kind === "list") {
    entry.refresh(value);
  } else {
    node.value = value === null || value === undefined ? "" : String(value);
  }
}

/**
 * schema 驱动的配置表单。
 *  - 控件类型按 schema 的 type 选：开关 / 数字 / 列表 / 下拉 / 文本 / 密钥
 *  - options 渲染成下拉，min / max / step 传给数字输入
 *  - 保存后以服务端回读值为准回填，错误按字段显示在控件下方
 */
function schemaConfigForm(data, path, options = {}) {
  const draft = { ...(data.values || {}) };
  const secrets = { ...(data.secrets || {}) };
  const controls = new Map();
  const fields = h("fieldset", { class: "plugin-config-fields", "aria-label": "插件配置" });
  const errorBoxes = new Map();

  const renderField = (spec) => {
    const key = String(spec.key || "").trim();
    if (!key) return;
    const label = spec.label || spec.description || key;
    const onChange = (value) => { draft[key] = value; };
    const entry = schemaFieldControl(spec, draft[key], onChange, secrets);
    controls.set(key, entry);
    entry.node.setAttribute("aria-label", label);
    const error = h("span", { class: "field-error", role: "alert" });
    errorBoxes.set(key, error);
    const hint = [];
    if (spec.description) hint.push(spec.description);
    if (spec.min !== undefined || spec.max !== undefined) {
      const low = spec.min === undefined ? "" : spec.min;
      const high = spec.max === undefined ? "" : spec.max;
      hint.push(`范围 ${low} – ${high}`);
    }
    if (spec.secret) hint.push("密钥不会回显，留空表示保留原值。");
    if (Array.isArray(spec.options) && spec.options.length) {
      hint.push(`可选：${spec.options.map((option) => option.label || option.value).join(" / ")}`);
    }
    fields.append(
      h(
        "div",
        { class: "field" },
        h("span", { class: "field-label" }, label),
        entry.node,
        hint.length ? h("span", { class: "field-hint" }, hint.join(" ")) : null,
        error
      )
    );
  };

  (data.schema || []).forEach(renderField);
  const save = h("button", { type: "submit", class: "btn btn-primary" }, icon("check"), "保存配置");
  const hot = Boolean(options.hot);
  const result = h(
    "div",
    { role: "status", class: "field-hint" },
    hot ? "保存后立即生效，无需重启。" : "保存后重启 bot 生效。"
  );
  const setErrors = (errors) => {
    for (const box of errorBoxes.values()) box.textContent = "";
    for (const item of errors || []) {
      const box = errorBoxes.get(item.field);
      if (box) box.textContent = item.message || "不合规";
    }
  };
  const form = h(
    "form",
    { class: "grid" },
    fields,
    h("div", { class: "market-actions" }, save, result)
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    save.disabled = fields.disabled = true;
    setErrors([]);
    try {
      const payload = await api.put(path, { values: draft });
      result.textContent = payload.message || "插件配置已保存。";
      // 服务端会归一化（去空项、转数字）并隐藏密钥；以回读值为准回填。
      // 只回填服务端确实返回的键：缺失的键保留原控件内容，不能凭空清空
      const fresh = payload.values || {};
      for (const [key, entry] of controls) {
        if (!Object.hasOwn(fresh, key)) continue;
        draft[key] = fresh[key];
        applySchemaValue(entry, fresh[key]);
      }
      if (payload.secrets) {
        Object.assign(secrets, payload.secrets);
        for (const [key, entry] of controls) {
          if (entry.kind === "secret" && entry.node) {
            entry.node.value = "";
            entry.node.placeholder = payload.secrets[key] ? "已设置，留空保留原值" : "尚未设置";
          }
        }
      }
    } catch (err) {
      if (!(err instanceof Unauthorized)) {
        result.textContent = err.message || "保存失败，请重试。";
        setErrors(err.fields || []);
      }
    } finally {
      save.disabled = fields.disabled = false;
    }
  });
  return form;
}

/** 详情页主体：标题、元信息、介绍、触发、权限、配置说明与配置表单。 */
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
                ? `共声明 ${data.permissions.declared.length} 项，已授权 ${(data.permissions.granted || []).length} 项。未授权的能力不会注入，也不能调用。`
                : `共声明 ${data.permissions.declared.length} 项；安装后到「权限授权」页逐项批准。`
            )
          : null,
        pluginDetailPermissions(data.permissions || {}),
      ),
    }
  );
  const docs = card("配置说明", { body: pluginDetailSchemaDocs(data.schema_docs) });

  const configCard = data.can_configure
    ? card("配置", {
        hint: data.hot_config ? "插件声明了 on_config_change，保存后热生效" : "保存后需重启 bot 生效",
        body: schemaConfigForm(
          { schema: data.schema, values: data.config.values || {}, secrets: data.config.secrets || {} },
          `/api/plugins/${idOf}/config`,
          { hot: data.hot_config }
        ),
      })
    : data.installed && data.valid
    ? card("配置", { body: h("p", { class: "field-hint" }, "该插件没有声明配置项。") })
    : card("配置", {
        body: h(
          "p",
          { class: "field-hint" },
          data.installed
            ? data.invalid_reason || "该插件的 manifest 不合规，不能配置。"
            : "插件还没安装：安装后即可在这里配置。"
        ),
      });

  return [overview, h("div", { class: "grid grid-2" }, intro, triggers), h("div", { class: "grid grid-2" }, permissions, docs), configCard];
}

/** 详情路由：#/plugins/detail/<id>；左侧菜单可切换其它插件。 */
async function renderPluginDetail(panel, selected) {
  let items = [];
  try {
    const market = await api.get("/api/market");
    items = (market.items || []).map((item) => ({ id: item.id, name: item.name || item.id }));
  } catch (err) {
    if (err instanceof Unauthorized) return;
    try {
      const local = await api.get("/api/plugins");
      items = (local.items || []).map((item) => ({
        id: item.manifest?.id || item.name,
        name: item.manifest?.name || item.name,
      }));
    } catch (inner) {
      if (inner instanceof Unauthorized) return;
    }
  }
  let picked = "";
  try {
    picked = selected ? decodeURIComponent(selected) : "";
  } catch (_err) {
    // 地址栏里可能有手改坏的转义：当作没选中，退回第一个插件
    picked = "";
  }
  const currentId = picked || items[0]?.id;
  if (!currentId) {
    panel.replaceChildren(
      card("", { body: emptyState("plugins", "还没有可查看的插件", "到插件市场安装后，即可在这里查看详情。") })
    );
    return;
  }
  const editor = h("div", { class: "settings-content" }, skeleton(3));
  panel.replaceChildren(
    h(
      "div",
      { class: "grid" },
      h(
        "div",
        { class: "toolbar" },
        h("a", { class: "btn btn-sm btn-ghost", href: "#/plugins" }, icon("caret"), "返回插件市场"),
        h("span", { class: "field-hint" }, "详情只读；配置请在下方的表单里修改。")
      ),
      h("div", { class: "settings-layout" }, pluginDetailMenu(items, currentId), editor)
    )
  );
  try {
    const payload = await api.get(`/api/plugins/${encodeURIComponent(currentId)}/detail`);
    editor.replaceChildren(...pluginDetailBody(payload.plugin || {}));
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
