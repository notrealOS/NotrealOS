/* toplevel-watch: print the activated toplevel's app_id whenever it changes.
 * One line per change on stdout: "active <app_id>" or "none" (no window
 * activated, e.g. the Phosh overview is open). Uses wlr-foreign-toplevel-management.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wayland-client.h>
#include "wlr-foreign-toplevel-management-unstable-v1-client.h"

struct toplevel {
	struct zwlr_foreign_toplevel_handle_v1 *handle;
	char *app_id;
	int activated, pending_activated;
	struct toplevel *next;
};

static struct zwlr_foreign_toplevel_manager_v1 *manager;
static struct toplevel *toplevels;
static char *last;

static void report(void)
{
	const char *cur = NULL;
	for (struct toplevel *t = toplevels; t; t = t->next)
		if (t->activated) {
			cur = t->app_id ? t->app_id : "";
			break;
		}
	if (last && cur && !strcmp(last, cur))
		return;
	if (!last && !cur)
		return;
	free(last);
	last = cur ? strdup(cur) : NULL;
	if (cur)
		printf("active %s\n", cur);
	else
		printf("none\n");
	fflush(stdout);
}

static void h_title(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, const char *s) {}

static void h_app_id(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, const char *s)
{
	struct toplevel *t = d;
	free(t->app_id);
	t->app_id = strdup(s);
}

static void h_output(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, struct wl_output *o) {}

static void h_state(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, struct wl_array *state)
{
	struct toplevel *t = d;
	uint32_t *s;
	t->pending_activated = 0;
	wl_array_for_each(s, state)
		if (*s == ZWLR_FOREIGN_TOPLEVEL_HANDLE_V1_STATE_ACTIVATED)
			t->pending_activated = 1;
}

static void h_done(void *d, struct zwlr_foreign_toplevel_handle_v1 *h)
{
	struct toplevel *t = d;
	t->activated = t->pending_activated;
	report();
}

static void h_closed(void *d, struct zwlr_foreign_toplevel_handle_v1 *h)
{
	struct toplevel *t = d, **p;
	for (p = &toplevels; *p; p = &(*p)->next)
		if (*p == t) {
			*p = t->next;
			break;
		}
	zwlr_foreign_toplevel_handle_v1_destroy(h);
	free(t->app_id);
	free(t);
	report();
}

static void h_parent(void *d, struct zwlr_foreign_toplevel_handle_v1 *h,
		     struct zwlr_foreign_toplevel_handle_v1 *parent) {}

static const struct zwlr_foreign_toplevel_handle_v1_listener handle_listener = {
	.title = h_title,
	.app_id = h_app_id,
	.output_enter = h_output,
	.output_leave = h_output,
	.state = h_state,
	.done = h_done,
	.closed = h_closed,
	.parent = h_parent,
};

static void m_toplevel(void *d, struct zwlr_foreign_toplevel_manager_v1 *m,
		       struct zwlr_foreign_toplevel_handle_v1 *h)
{
	struct toplevel *t = calloc(1, sizeof(*t));
	t->handle = h;
	t->next = toplevels;
	toplevels = t;
	zwlr_foreign_toplevel_handle_v1_add_listener(h, &handle_listener, t);
}

static void m_finished(void *d, struct zwlr_foreign_toplevel_manager_v1 *m)
{
	exit(1);
}

static const struct zwlr_foreign_toplevel_manager_v1_listener manager_listener = {
	.toplevel = m_toplevel,
	.finished = m_finished,
};

static void r_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t version)
{
	if (!strcmp(iface, zwlr_foreign_toplevel_manager_v1_interface.name))
		manager = wl_registry_bind(r, name, &zwlr_foreign_toplevel_manager_v1_interface,
					   version < 3 ? version : 3);
}

static void r_global_remove(void *d, struct wl_registry *r, uint32_t name) {}

static const struct wl_registry_listener registry_listener = {
	.global = r_global,
	.global_remove = r_global_remove,
};

int main(void)
{
	struct wl_display *display = wl_display_connect(NULL);
	if (!display) {
		fprintf(stderr, "toplevel-watch: cannot connect to Wayland\n");
		return 1;
	}
	struct wl_registry *registry = wl_display_get_registry(display);
	wl_registry_add_listener(registry, &registry_listener, NULL);
	wl_display_roundtrip(display);
	if (!manager) {
		fprintf(stderr, "toplevel-watch: compositor lacks wlr-foreign-toplevel-management\n");
		return 1;
	}
	zwlr_foreign_toplevel_manager_v1_add_listener(manager, &manager_listener, NULL);
	wl_display_roundtrip(display);
	wl_display_roundtrip(display);
	if (!last) {
		printf("none\n");
		fflush(stdout);
	}
	while (wl_display_dispatch(display) != -1)
		;
	return 1;
}
