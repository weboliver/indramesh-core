import { useEffect as e, useRef as t, useState as n } from "react";
import { useNavigate as r } from "react-router-dom";
import { useQuery as i } from "@tanstack/react-query";
import { Button as a, CheckboxField as o, FormField as s, NumberInput as c, SelectInput as l, api as u, interpolateDefault as d, showToast as f, usePluginTranslation as p, useSchemas as m, useTasks as h } from "@indramesh/plugin-api";
import { jsx as g, jsxs as _ } from "react/jsx-runtime";
//#region locales/en.json
var v = {
	"section.object.title": "Example details",
	"section.object.body": "This section comes from the example plugin. It shows object \"{{name}}\".",
	"section.schema.title": "Example schema settings",
	"section.schema.body": "Shown for schema \"{{name}}\".",
	"section.object_type.title": "Example object type settings",
	"section.object_type.body": "Shown for object type \"{{name}}\".",
	"wizard.entry.label": "Use example schema draft",
	"translation.panel.label": "Fill example translation drafts",
	"translation.panel.demo_value": "Example draft for {{field}} ({{language}})",
	"translation.admin.label": "Inspect translation scope",
	"task.detail.label": "Show example task context",
	"badge.label": "Example",
	"badge.title": "Marked by the example plugin",
	"task.schema": "Schema",
	"task.schema_placeholder": "Select a schema…",
	"task.batch_size": "Batch size",
	"task.batch_size_help": "Number of assets checked per step.",
	"task.fail": "Simulate a failure",
	"connector.label": "Example Connector",
	"connector.page_size": "Page size",
	"connector.page_size_help": "Records fetched per request.",
	"nav.label": "Example"
}, y = {
	"section.object.title": "Beispieldetails",
	"section.object.body": "Dieser Abschnitt stammt aus dem Beispiel-Plugin. Er zeigt das Objekt „{{name}}“.",
	"section.schema.title": "Beispiel-Schemaeinstellungen",
	"section.schema.body": "Angezeigt für das Schema „{{name}}“.",
	"section.object_type.title": "Beispiel-Einstellungen für den Objekttyp",
	"section.object_type.body": "Angezeigt für den Objekttyp „{{name}}“.",
	"wizard.entry.label": "Beispiel-Schema-Entwurf verwenden",
	"translation.panel.label": "Beispiel-Übersetzungsentwürfe einfügen",
	"translation.panel.demo_value": "Beispielentwurf für {{field}} ({{language}})",
	"translation.admin.label": "Übersetzungsbereich prüfen",
	"task.detail.label": "Beispiel-Aufgabenkontext anzeigen",
	"badge.label": "Beispiel",
	"badge.title": "Vom Beispiel-Plugin markiert",
	"task.schema": "Schema",
	"task.schema_placeholder": "Schema auswählen…",
	"task.batch_size": "Batchgröße",
	"task.batch_size_help": "Anzahl der Assets, die pro Schritt geprüft werden.",
	"task.fail": "Fehler simulieren",
	"connector.label": "Beispiel-Connector",
	"connector.page_size": "Seitengröße",
	"connector.page_size_help": "Datensätze pro Abruf.",
	"nav.label": "Beispiel"
};
//#endregion
//#region index.jsx
function b() {
	let [e, t] = n(0), o = r(), { data: s } = i({
		queryKey: ["plugin-example-ping"],
		queryFn: async () => "ok"
	});
	return /* @__PURE__ */ _("div", {
		className: "space-y-4",
		"data-testid": "plugin-page-example",
		children: [
			/* @__PURE__ */ g("h1", {
				className: "text-lg font-semibold",
				children: "Example plugin dashboard"
			}),
			/* @__PURE__ */ g("p", {
				className: "text-sm text-muted-foreground",
				children: "This page is rendered by the plugin bundle; React, the router and the QueryClient are the host's own instances."
			}),
			/* @__PURE__ */ _("p", {
				"data-testid": "plugin-example-ping",
				children: ["Query result: ", s ?? "pending"]
			}),
			/* @__PURE__ */ _("p", {
				"data-testid": "plugin-example-counter",
				children: ["Counter: ", e]
			}),
			/* @__PURE__ */ g(a, {
				"data-testid": "plugin-example-increment",
				onClick: () => t((e) => e + 1),
				children: "Increment"
			}),
			/* @__PURE__ */ g(a, {
				"data-testid": "plugin-example-navigate",
				onClick: () => o("/search"),
				children: "Go to search"
			})
		]
	});
}
function x({ attribute: e, value: t, onChange: n, error: r }) {
	let i = e?.name || "URL";
	return /* @__PURE__ */ _("div", {
		className: "space-y-1.5",
		"data-testid": "plugin-attribute-editor-example",
		children: [/* @__PURE__ */ g("input", {
			type: "url",
			value: t ?? "",
			onChange: (e) => n(e.target.value),
			"aria-invalid": r ? !0 : void 0,
			"aria-label": i,
			placeholder: "https://example.com",
			className: `w-full rounded-md border px-3 py-2 text-sm bg-background ${r ? "border-destructive" : "border-input"} focus:outline-none focus:ring-1 focus:ring-primary`
		}), /* @__PURE__ */ g(a, {
			type: "button",
			variant: "outline",
			size: "sm",
			onClick: () => n(""),
			children: "Clear"
		})]
	});
}
function S({ objectType: e }) {
	return /* @__PURE__ */ g(a, {
		type: "button",
		variant: "outline",
		size: "sm",
		"data-testid": "plugin-action-button-example",
		onClick: () => f(`Example plugin action for ${e?.name ?? "this Object Type"}`),
		children: "Example action"
	});
}
function C({ onSelect: e }) {
	let { t } = p("example");
	return /* @__PURE__ */ g(a, {
		type: "button",
		variant: "outline",
		size: "sm",
		"data-testid": "plugin-schema-wizard-entry-example",
		className: "h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold",
		onClick: () => e?.(),
		children: t("wizard.entry.label", "Use example schema draft")
	});
}
function w({ entityType: n, entityId: r, eligiblePairs: i = [], sessionId: o, scopeRevision: s, registerDraftHandlers: c, acceptDraft: l, getCurrentEligiblePairs: f, canWrite: m, disabled: h }) {
	let { t: _ } = p("example"), v = t(/* @__PURE__ */ new Set());
	e(() => {
		if (typeof c != "function") return;
		let e = (e, t) => `${e}\u0000${t}`;
		return c({
			commitDraft: async ({ value: t, field: i, language: a }) => {
				if (!v.current.has(e(i, a))) return { status: "invalid" };
				try {
					let o = (await u.post("plugins/example/demo-translation/apply", {
						translation_type: n,
						entity_id: r,
						field: i,
						language_code: a,
						value: t
					}))?.data?.status;
					return o === "written" || o === "skipped" ? (v.current.delete(e(i, a)), { status: o }) : { status: "invalid" };
				} catch {
					return { status: "invalid" };
				}
			},
			discardDraft: async ({ field: t, language: n }) => (v.current.delete(e(t, n)), !0)
		});
	}, [
		c,
		n,
		r
	]);
	let y = h || m === !1;
	return /* @__PURE__ */ g(a, {
		type: "button",
		variant: "outline",
		size: "sm",
		"data-testid": "plugin-translation-panel-action-example",
		className: "h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold",
		disabled: y,
		onClick: () => {
			if (y || typeof l != "function") return;
			let e = typeof f == "function" ? f() : i, t = Array.isArray(e) ? e : [];
			t.length !== 0 && l({
				sessionId: o,
				scopeRevision: s,
				proposals: t.map((e) => (v.current.add(`${e.field}\u0000${e.language}`), {
					field: e.field,
					language: e.language,
					value: d(_("translation.panel.demo_value", "Example draft for {{field}} ({{language}})"), {
						field: e.field,
						language: e.language
					}),
					reference: `${e.field}:${e.language}`
				}))
			});
		},
		children: _("translation.panel.label", "Fill example translation drafts")
	});
}
function T({ onInspect: e }) {
	let { t } = p("example");
	return /* @__PURE__ */ g(a, {
		type: "button",
		variant: "outline",
		size: "sm",
		"data-testid": "plugin-translation-admin-action-example",
		className: "h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold",
		onClick: () => e?.(),
		children: t("translation.admin.label", "Inspect translation scope")
	});
}
function E({ onToggle: e }) {
	let { t } = p("example");
	return /* @__PURE__ */ g(a, {
		type: "button",
		variant: "outline",
		size: "sm",
		"data-testid": "plugin-task-detail-action-example",
		className: "h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold",
		onClick: () => e?.(),
		children: t("task.detail.label", "Show example task context")
	});
}
function D() {
	return /* @__PURE__ */ _("div", {
		className: "rounded-lg border p-6 space-y-4",
		"data-testid": "plugin-settings-section-example",
		children: [/* @__PURE__ */ g("h2", {
			className: "text-lg font-semibold",
			children: "Example plugin settings"
		}), /* @__PURE__ */ g("p", {
			className: "text-sm text-muted-foreground",
			children: "This section is appended by the example plugin's bundle. It receives no props — a settings section owns its data, exactly like a page."
		})]
	});
}
function O({ object: e }) {
	let { t } = p("example"), n = t("section.object.body", "This section comes from the example plugin. It shows object \"{{name}}\".");
	return /* @__PURE__ */ g("p", {
		className: "text-sm text-muted-foreground",
		"data-testid": "plugin-example-object-section",
		children: d(n, { name: e?.name ?? "" })
	});
}
function k({ schema: e }) {
	let { t } = p("example"), n = t("section.schema.body", "Shown for schema \"{{name}}\".");
	return /* @__PURE__ */ g("p", {
		className: "text-sm text-muted-foreground",
		"data-testid": "plugin-example-schema-section",
		children: d(n, { name: e?.name ?? "" })
	});
}
function A({ objectType: e }) {
	let { t } = p("example"), n = t("section.object_type.body", "Shown for object type \"{{name}}\".");
	return /* @__PURE__ */ g("p", {
		className: "text-sm text-muted-foreground",
		"data-testid": "plugin-example-object-type-section",
		children: d(n, { name: e?.name ?? "" })
	});
}
function j({ value: t, onChange: n, disabled: r }) {
	let { t: i } = p("example"), a = t?.scope?.schema_id ?? "", u = t?.plugin_parameters?.batch_size ?? 10, d = t?.plugin_parameters?.fail ?? !1, { data: f } = m(null, 0, 200, { enabled: !0 }), h = (e) => {
		n({
			scope: { schema_id: e.schemaId },
			plugin_parameters: {
				batch_size: Number(e.batchSize),
				fail: e.fail
			},
			valid: !!e.schemaId && Number(e.batchSize) >= 1
		});
	};
	e(() => {
		h({
			schemaId: "",
			batchSize: 10,
			fail: !1
		});
	}, []);
	let v = (f ?? []).map((e) => ({
		value: e.id,
		label: e.name
	}));
	return /* @__PURE__ */ _("div", {
		className: "space-y-4",
		"data-testid": "plugin-example-task-form",
		children: [
			/* @__PURE__ */ g(s, {
				id: "plugin-example-task-schema",
				label: i("task.schema", "Schema"),
				required: !0,
				children: /* @__PURE__ */ g(l, {
					id: "plugin-example-task-schema",
					"data-testid": "plugin-example-task-schema",
					value: a,
					placeholder: i("task.schema_placeholder", "Select a schema…"),
					options: v,
					disabled: r,
					onChange: (e) => h({
						schemaId: e.target.value,
						batchSize: u,
						fail: d
					})
				})
			}),
			/* @__PURE__ */ g(s, {
				id: "plugin-example-task-batch",
				label: i("task.batch_size", "Batch size"),
				help: i("task.batch_size_help", "Number of assets checked per step."),
				children: /* @__PURE__ */ g(c, {
					id: "plugin-example-task-batch",
					"data-testid": "plugin-example-task-batch",
					min: 1,
					value: u,
					disabled: r,
					onChange: (e) => h({
						schemaId: a,
						batchSize: e.target.value,
						fail: d
					})
				})
			}),
			/* @__PURE__ */ g(o, {
				id: "plugin-example-task-fail",
				"data-testid": "plugin-example-task-fail",
				label: i("task.fail", "Simulate a failure"),
				checked: d,
				disabled: r,
				onChange: (e) => h({
					schemaId: a,
					batchSize: u,
					fail: e.target.checked
				})
			})
		]
	});
}
function M({ value: e, onChange: t }) {
	let { t: n } = p("example"), r = e?.page_size ?? 25;
	return /* @__PURE__ */ g(s, {
		id: "plugin-example-page-size",
		label: n("connector.page_size", "Page size"),
		help: n("connector.page_size_help", "Records fetched per request."),
		children: /* @__PURE__ */ g(c, {
			id: "plugin-example-page-size",
			"data-testid": "plugin-example-page-size",
			min: 1,
			value: r,
			onChange: (n) => t({
				...e,
				page_size: Number(n.target.value)
			})
		})
	});
}
async function N({ ids: e }) {
	await u.get("plugins/example/ping", { params: { ids: e.join(",") } });
	let t = /* @__PURE__ */ new Map();
	return e.forEach((e) => {
		t.set(e, [{
			label: "Example",
			labelKey: "badge.label",
			title: "Marked by the example plugin",
			titleKey: "badge.title",
			icon: "puzzle"
		}]);
	}), t;
}
function P() {
	return h(0, 1, "example.cleanup-assets").data?.total;
}
var F = {
	translations: {
		en: v,
		de: y
	},
	pages: [{
		id: "example-dashboard",
		path: "dashboard",
		component: b
	}],
	navItems: [{
		id: "example-nav",
		label: "Example",
		labelKey: "nav.label",
		path: "/plugins/example/dashboard",
		icon: "puzzle",
		group: "Example"
	}, {
		id: "example-nav-tools",
		label: "Example",
		labelKey: "nav.label",
		path: "/plugins/example/dashboard",
		icon: "puzzle",
		group: "Tools",
		permission: "sync:trigger",
		badge: P
	}],
	attributeEditors: [{
		id: "example-url-editor",
		data_type: "url",
		component: x
	}],
	actionButtons: [{
		id: "example-action",
		objectType: "*",
		component: S
	}],
	settingsSections: [{
		id: "example-settings",
		label: "Example",
		component: D
	}],
	schemaWizardEntries: [{
		id: "example-schema-wizard-entry",
		label: "Use example schema draft",
		labelKey: "wizard.entry.label",
		permission: "example-tools:read",
		component: C
	}],
	translationPanelActions: [{
		id: "example-translation-panel-action",
		label: "Fill example translation drafts",
		labelKey: "translation.panel.label",
		permission: "example-tools:read",
		component: w
	}],
	translationAdminActions: [{
		id: "example-translation-admin-action",
		label: "Inspect translation scope",
		labelKey: "translation.admin.label",
		permission: "example-tools:read",
		component: T
	}],
	taskDetailActions: [{
		id: "example-task-detail-action",
		label: "Show example task context",
		labelKey: "task.detail.label",
		permission: "example-tools:read",
		component: E
	}],
	objectDetailSections: [{
		id: "example-object-details",
		label: "Example details",
		labelKey: "section.object.title",
		component: O
	}],
	schemaSettingsSections: [{
		id: "example-schema-settings",
		label: "Example schema settings",
		labelKey: "section.schema.title",
		component: k
	}],
	objectTypeSettingsSections: [{
		id: "example-object-type-settings",
		label: "Example object type settings",
		labelKey: "section.object_type.title",
		component: A
	}],
	itemBadges: [{
		id: "example-object-badge",
		kinds: ["object"],
		permission: "sync:trigger",
		resolve: N
	}],
	taskForms: [{
		id: "example-cleanup-form",
		tasktype: "example.cleanup-assets",
		component: j
	}],
	connectorConfigEditors: [{
		id: "example-connector-editor",
		connector_type: "example",
		label: "Example Connector",
		labelKey: "connector.label",
		roles: ["source"],
		component: M
	}]
};
//#endregion
export { F as default };
