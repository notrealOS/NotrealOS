/* swipe-im: tiny Wayland input-method-v2 + virtual-keyboard bridge for swipekbd.py
 *
 * stdout (events, one per line):
 *   A                       input method activated (text field focused)
 *   X                       deactivated
 *   T <hint> <purpose>      content type
 *   S <cursor> <anchor> <text>   surrounding text (\\ and \n escaped)
 *   U                       input method unavailable (another OSK holds it)
 * stdin (commands, one per line):
 *   C <text>                commit string
 *   P <text>                set preedit (cursor at end); "P" alone clears it
 *   D <before> <after>      delete surrounding text (bytes)
 *   K <evdev code> <mods>   press+release a key (mods: 1 shift, 4 ctrl, 8 alt)
 *   W <x> <y> <xext> <yext> <dy>  virtual pointer: move to x/xext, y/yext of the
 *                           output layout, then scroll dy (finger source, + = down)
 *   w                       end the scroll (axis_stop: kinetic scrolling starts)
 *                           and drop the virtual pointer VP_LINGER_MS later, so phoc
 *                           hides the cursor. Not at once: removing the seat's pointer
 *                           while GTK4 is still kinetic-scrolling segfaults the app.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "input-method-unstable-v2-client-protocol.h"
#include "virtual-keyboard-unstable-v1-client-protocol.h"
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

static struct wl_seat *seat;
static struct zwp_input_method_manager_v2 *im_mgr;
static struct zwp_virtual_keyboard_manager_v1 *vk_mgr;
static struct zwp_input_method_v2 *im;
static struct zwp_virtual_keyboard_v1 *vk;
static struct zwlr_virtual_pointer_manager_v1 *vp_mgr;
static struct zwlr_virtual_pointer_v1 *vp;
#define VP_LINGER_MS 5000
static uint32_t vp_expire;  /* when to destroy vp after a scroll ends; 0 = scrolling */
static uint32_t serial;
static int active, pending_active;
static char *pending_text;
static uint32_t pending_cursor, pending_anchor, pending_hint, pending_purpose;
static int have_text, have_type;

static void out_escaped(const char *s)
{
	for (; *s; s++) {
		if (*s == '\\') fputs("\\\\", stdout);
		else if (*s == '\n') fputs("\\n", stdout);
		else putchar(*s);
	}
}

static void im_activate(void *d, struct zwp_input_method_v2 *m) { pending_active = 1; }
static void im_deactivate(void *d, struct zwp_input_method_v2 *m) { pending_active = 0; }
static void im_surrounding(void *d, struct zwp_input_method_v2 *m, const char *text,
			   uint32_t cursor, uint32_t anchor)
{
	free(pending_text);
	pending_text = strdup(text ? text : "");
	pending_cursor = cursor;
	pending_anchor = anchor;
	have_text = 1;
}
static void im_cause(void *d, struct zwp_input_method_v2 *m, uint32_t cause) {}
static void im_content_type(void *d, struct zwp_input_method_v2 *m, uint32_t hint, uint32_t purpose)
{
	pending_hint = hint;
	pending_purpose = purpose;
	have_type = 1;
}
static void im_done(void *d, struct zwp_input_method_v2 *m)
{
	serial++;
	if (pending_active != active) {
		active = pending_active;
		puts(active ? "A" : "X");
	}
	if (active && have_type)
		printf("T %u %u\n", pending_hint, pending_purpose);
	if (active && have_text) {
		printf("S %u %u ", pending_cursor, pending_anchor);
		out_escaped(pending_text);
		putchar('\n');
	}
	have_text = have_type = 0;
	fflush(stdout);
}
static void im_unavailable(void *d, struct zwp_input_method_v2 *m)
{
	puts("U");
	fflush(stdout);
	exit(3);
}
static const struct zwp_input_method_v2_listener im_listener = {
	.activate = im_activate,
	.deactivate = im_deactivate,
	.surrounding_text = im_surrounding,
	.text_change_cause = im_cause,
	.content_type = im_content_type,
	.done = im_done,
	.unavailable = im_unavailable,
};

static void reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver)
{
	if (!strcmp(iface, wl_seat_interface.name) && !seat)
		seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
	else if (!strcmp(iface, zwp_input_method_manager_v2_interface.name))
		im_mgr = wl_registry_bind(r, name, &zwp_input_method_manager_v2_interface, 1);
	else if (!strcmp(iface, zwp_virtual_keyboard_manager_v1_interface.name))
		vk_mgr = wl_registry_bind(r, name, &zwp_virtual_keyboard_manager_v1_interface, 1);
	else if (!strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name))
		vp_mgr = wl_registry_bind(r, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

static void upload_keymap(void)
{
	struct xkb_context *ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	struct xkb_rule_names names = { .layout = "us" };
	struct xkb_keymap *km = xkb_keymap_new_from_names(ctx, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
	char *s = xkb_keymap_get_as_string(km, XKB_KEYMAP_FORMAT_TEXT_V1);
	size_t len = strlen(s) + 1;
	int fd = memfd_create("swipe-keymap", MFD_CLOEXEC);
	if (fd < 0 || ftruncate(fd, len) < 0) { perror("keymap"); exit(1); }
	void *p = mmap(NULL, len, PROT_WRITE, MAP_SHARED, fd, 0);
	memcpy(p, s, len);
	munmap(p, len);
	zwp_virtual_keyboard_v1_keymap(vk, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fd, len);
	close(fd);
	free(s);
	xkb_keymap_unref(km);
	xkb_context_unref(ctx);
}

static uint32_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void unescape(char *s)
{
	char *o = s;
	for (; *s; s++) {
		if (*s == '\\' && s[1] == 'n') { *o++ = '\n'; s++; }
		else if (*s == '\\' && s[1] == '\\') { *o++ = '\\'; s++; }
		else *o++ = *s;
	}
	*o = 0;
}

static void handle(char *line)
{
	char *arg = line[0] && line[1] == ' ' ? line + 2 : line + 1;
	switch (line[0]) {
	case 'C':
		unescape(arg);
		zwp_input_method_v2_commit_string(im, arg);
		zwp_input_method_v2_commit(im, serial);
		break;
	case 'P':
		unescape(arg);
		zwp_input_method_v2_set_preedit_string(im, arg, strlen(arg), strlen(arg));
		zwp_input_method_v2_commit(im, serial);
		break;
	case 'R': {	/* R <bytes before> <text>: delete + commit atomically */
		unsigned b = 0; int off = 0;
		sscanf(arg, "%u %n", &b, &off);
		char *t = arg + off;
		unescape(t);
		zwp_input_method_v2_delete_surrounding_text(im, b, 0);
		zwp_input_method_v2_commit_string(im, t);
		zwp_input_method_v2_commit(im, serial);
		break;
	}
	case 'D': {
		unsigned b = 0, a = 0;
		sscanf(arg, "%u %u", &b, &a);
		zwp_input_method_v2_delete_surrounding_text(im, b, a);
		zwp_input_method_v2_commit(im, serial);
		break;
	}
	case 'K': {
		unsigned code = 0, mods = 0;
		sscanf(arg, "%u %u", &code, &mods);
		if (mods) zwp_virtual_keyboard_v1_modifiers(vk, mods, 0, 0, 0);
		zwp_virtual_keyboard_v1_key(vk, now_ms(), code, WL_KEYBOARD_KEY_STATE_PRESSED);
		zwp_virtual_keyboard_v1_key(vk, now_ms(), code, WL_KEYBOARD_KEY_STATE_RELEASED);
		if (mods) zwp_virtual_keyboard_v1_modifiers(vk, 0, 0, 0, 0);
		break;
	}
	case 'W': {
		unsigned x = 0, y = 0, xe = 1, ye = 1;
		double dy = 0;
		if (!vp_mgr || sscanf(arg, "%u %u %u %u %lf", &x, &y, &xe, &ye, &dy) != 5) break;
		if (!vp)  /* only exists while scrolling: a lingering pointer keeps phoc's cursor up */
			vp = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(vp_mgr, seat);
		vp_expire = 0;
		uint32_t t = now_ms();
		zwlr_virtual_pointer_v1_motion_absolute(vp, t, x, y, xe, ye);
		zwlr_virtual_pointer_v1_axis_source(vp, WL_POINTER_AXIS_SOURCE_FINGER);
		zwlr_virtual_pointer_v1_axis(vp, t, WL_POINTER_AXIS_VERTICAL_SCROLL, wl_fixed_from_double(dy));
		zwlr_virtual_pointer_v1_frame(vp);
		break;
	}
	case 'w':
		if (!vp) break;
		zwlr_virtual_pointer_v1_axis_source(vp, WL_POINTER_AXIS_SOURCE_FINGER);
		zwlr_virtual_pointer_v1_axis_stop(vp, now_ms(), WL_POINTER_AXIS_VERTICAL_SCROLL);
		zwlr_virtual_pointer_v1_frame(vp);
		vp_expire = now_ms() + VP_LINGER_MS;
		if (!vp_expire) vp_expire = 1;
		break;
	}
}

int main(void)
{
	struct wl_display *dpy = wl_display_connect(NULL);
	if (!dpy) { fprintf(stderr, "no wayland display\n"); return 1; }
	struct wl_registry *reg = wl_display_get_registry(dpy);
	wl_registry_add_listener(reg, &reg_listener, NULL);
	wl_display_roundtrip(dpy);
	if (!seat || !im_mgr || !vk_mgr) {
		fprintf(stderr, "missing globals: seat=%p im=%p vk=%p\n", (void *)seat, (void *)im_mgr, (void *)vk_mgr);
		return 2;
	}
	im = zwp_input_method_manager_v2_get_input_method(im_mgr, seat);
	zwp_input_method_v2_add_listener(im, &im_listener, NULL);
	vk = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(vk_mgr, seat);
	upload_keymap();
	wl_display_roundtrip(dpy);
	fputs("R\n", stdout);
	fflush(stdout);

	char buf[65536];
	size_t used = 0;
	struct pollfd fds[2] = { { wl_display_get_fd(dpy), POLLIN }, { 0, POLLIN } };
	for (;;) {
		while (wl_display_prepare_read(dpy) != 0)
			wl_display_dispatch_pending(dpy);
		wl_display_flush(dpy);
		int timeout = -1;
		if (vp && vp_expire) {
			int32_t left = (int32_t)(vp_expire - now_ms());
			timeout = left > 0 ? left : 0;
		}
		if (poll(fds, 2, timeout) < 0 && errno != EINTR) { wl_display_cancel_read(dpy); break; }
		if (fds[0].revents & POLLIN) wl_display_read_events(dpy);
		else wl_display_cancel_read(dpy);
		if (fds[0].revents & (POLLERR | POLLHUP)) break;
		if (wl_display_dispatch_pending(dpy) < 0) break;
		if (vp && vp_expire && (int32_t)(now_ms() - vp_expire) >= 0) {
			zwlr_virtual_pointer_v1_destroy(vp);
			vp = NULL;
			vp_expire = 0;
		}
		if (fds[1].revents & (POLLIN | POLLHUP)) {
			ssize_t n = read(0, buf + used, sizeof(buf) - 1 - used);
			if (n <= 0) break;
			used += n;
			char *start = buf, *nl;
			while ((nl = memchr(start, '\n', buf + used - start))) {
				*nl = 0;
				handle(start);
				start = nl + 1;
			}
			used -= start - buf;
			memmove(buf, start, used);
			if (used == sizeof(buf) - 1) used = 0;
		}
	}
	return 0;
}
