"""Calendars through Evolution Data Server: Google (CalDAV via GNOME Online Accounts),
outlook.com + Microsoft 365 (evolution-ews). Evolution syncs our changes back to each service.

Events come out as plain dicts; recurring events are expanded into one dict per occurrence.
"""
import datetime
import os
import threading

import gi
gi.require_version("ECal", "2.0")
gi.require_version("EDataServer", "1.2")
gi.require_version("ICalGLib", "3.0")
from gi.repository import ECal, EDataServer, GLib, ICalGLib  # noqa: E402


def local_tzid():
    try:
        return os.path.realpath("/etc/localtime").split("/zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "Europe/Stockholm"


TZ = ICalGLib.Timezone.get_builtin_timezone(local_tzid())


def reminders(comp):
    """Minutes BEFORE the start of each relative VALARM (negative = after the start), sorted.
    Alarms relative to the end or at a fixed time are skipped."""
    out = []
    a = comp.get_first_component(ICalGLib.ComponentKind.VALARM_COMPONENT)
    while a:
        p = a.get_first_property(ICalGLib.PropertyKind.TRIGGER_PROPERTY)
        trig = p.get_trigger() if p else None
        related = p.get_first_parameter(ICalGLib.ParameterKind.RELATED_PARAMETER) if p else None
        if trig and trig.get_time().is_null_time() and not (related and "END" in related.as_ical_string()):
            out.append(-trig.get_duration().as_int() // 60)
        a = comp.get_next_component(ICalGLib.ComponentKind.VALARM_COMPONENT)
    return sorted(set(out), reverse=True)


def _when(itime):
    """ICalGLib.Time -> (local datetime, all_day)."""
    if itime is None or itime.is_null_time():
        return None, False
    if itime.is_date():
        return datetime.datetime(itime.get_year(), itime.get_month(), itime.get_day()), True
    t = itime.as_timet_with_zone(itime.get_timezone() or (ICalGLib.Timezone.get_utc_timezone()
                                                          if itime.is_utc() else TZ))
    return datetime.datetime.fromtimestamp(t), False


class Calendar:
    def __init__(self, src, parent_name):
        ext = src.get_extension(EDataServer.SOURCE_EXTENSION_CALENDAR)
        self.uid = src.get_uid()
        self.name = src.get_display_name()
        self.account = parent_name
        self.color = ext.get_color() or "#8fe0f2"
        self.backend = ext.get_backend_name()
        self.src = src
        self.client = None
        self.readonly = True

    def connect(self):
        # 1 s: 0 means "wait until online" (it hung in Contacts on 10-16)
        self.client = ECal.Client.connect_sync(self.src, ECal.ClientSourceType.EVENTS, 1, None)
        self.readonly = self.client.is_readonly()

    @property
    def service(self):
        a = (self.account or "").lower()
        if self.backend == "caldav":
            return "Google"
        if "outlook" in a or "hotmail" in a:
            return "Outlook"
        if self.backend == "microsoft365":
            return "Microsoft 365"
        return "Phone"


class Store:
    def __init__(self):
        self.registry = EDataServer.SourceRegistry.new_sync(None)
        self.cals = []
        for src in self.registry.list_enabled(EDataServer.SOURCE_EXTENSION_CALENDAR):
            if src.get_uid() == "birthdays":          # contact birthdays: duplicates the services' own
                continue
            parent = self.registry.ref_source(src.get_parent()) if src.get_parent() else None
            self.cals.append(Calendar(src, parent.get_display_name() if parent else ""))

    def connect_all(self):
        def one(c):
            try:
                c.connect()
            except GLib.Error as e:
                print("cal: connect", c.name, e.message, flush=True)
        ths = [threading.Thread(target=one, args=(c,)) for c in self.cals]
        for t in ths:
            t.start()
        for t in ths:
            t.join(30)

    def by_uid(self, uid):
        return next((c for c in self.cals if c.uid == uid), None)

    def writable(self):
        return [c for c in self.cals if c.client and not c.readonly]

    def events(self, start, end, hidden=()):
        """Occurrences between two datetimes, sorted. Each: dict(cal, uid, rid, title, start, end,
        all_day, location, notes, remind (minutes before), recurring)."""
        t0, t1 = int(start.timestamp()), int(end.timestamp())
        out, lock = [], threading.Lock()

        def one(c):
            if not c.client or c.uid in hidden:
                return
            found = []

            def cb(comp, istart, iend, *_):
                s, all_day = _when(comp.get_dtstart())
                e, _ = _when(comp.get_dtend())
                # instance times come as unix times for this occurrence
                if isinstance(istart, int):
                    s = datetime.datetime.fromtimestamp(istart) if not all_day else \
                        datetime.datetime.fromtimestamp(istart).replace(hour=0, minute=0)
                    e = datetime.datetime.fromtimestamp(iend) if isinstance(iend, int) else e
                rid = comp.get_recurrenceid()
                found.append({
                    "cal": c.uid, "uid": comp.get_uid(),
                    "rid": rid.as_ical_string() if rid and not rid.is_null_time() else None,
                    "title": comp.get_summary() or "(no title)", "start": s, "end": e or s,
                    "all_day": all_day, "location": comp.get_location() or "",
                    "notes": comp.get_description() or "",
                    "remind": reminders(comp),
                    "recurring": bool(comp.get_first_property(ICalGLib.PropertyKind.RRULE_PROPERTY)) or bool(rid),
                })
                return True
            try:
                c.client.generate_instances_sync(t0, t1, None, cb)
            except GLib.Error as e:
                print("cal: read", c.name, e.message, flush=True)
            with lock:
                out.extend(found)
        ths = [threading.Thread(target=one, args=(c,)) for c in self.cals]
        for t in ths:
            t.start()
        for t in ths:
            t.join(30)
        out.sort(key=lambda e: (e["start"], not e["all_day"], e["title"].lower()))
        return out

    # --- writing --------------------------------------------------------------------------------
    @staticmethod
    def _itime(dt, all_day):
        if all_day:
            t = ICalGLib.Time.new_null_date()
            t.set_date(dt.year, dt.month, dt.day)
            t.set_is_date(True)
            return t
        # UTC: EDS couldn't resolve libical's builtin TZID ("/freeassociation.../Europe/Stockholm")
        # and read local times back 2 h off (10-16)
        return ICalGLib.Time.new_from_timet_with_zone(int(dt.timestamp()), 0, ICalGLib.Timezone.get_utc_timezone())

    @staticmethod
    def _set_remind(comp, title, remind):
        """remind = minutes before the start (negative: after); "off" = no reminder; None = keep."""
        if remind is None:
            return
        old = []
        a = comp.get_first_component(ICalGLib.ComponentKind.VALARM_COMPONENT)
        while a:
            old.append(a)
            a = comp.get_next_component(ICalGLib.ComponentKind.VALARM_COMPONENT)
        for a in old:
            comp.remove_component(a)
        if remind == "off":
            return
        a = ICalGLib.Component.new_valarm()
        a.add_property(ICalGLib.Property.new_action(ICalGLib.PropertyAction.DISPLAY))
        a.add_property(ICalGLib.Property.new_trigger(ICalGLib.Trigger.new_from_int(-int(remind) * 60)))
        a.add_property(ICalGLib.Property.new_description(title))
        comp.add_component(a)

    def _fill(self, comp, title, start, end, all_day, location, notes):
        comp.set_summary(title)
        comp.set_dtstart(self._itime(start, all_day))
        if all_day and end.date() <= start.date():
            end = start + datetime.timedelta(days=1)       # DTEND is exclusive for all-day events
        comp.set_dtend(self._itime(end, all_day))
        comp.set_location(location or "")
        comp.set_description(notes or "")

    def create(self, cal_uid, title, start, end, all_day=False, location="", notes="", remind=None):
        cal = self.by_uid(cal_uid)
        comp = ICalGLib.Component.new_vevent()
        self._fill(comp, title, start, end, all_day, location, notes)
        self._set_remind(comp, title, remind)
        ok, uid = cal.client.create_object_sync(comp, ECal.OperationFlags.NONE, None)
        return uid

    def update(self, ev, title, start, end, all_day=False, location="", notes="", remind=None):
        cal = self.by_uid(ev["cal"])
        ok, comp = cal.client.get_object_sync(ev["uid"], ev["rid"], None)
        self._fill(comp, title, start, end, all_day, location, notes)
        self._set_remind(comp, title, remind)
        cal.client.modify_object_sync(comp, ECal.ObjModType.THIS if ev["rid"] else ECal.ObjModType.ALL,
                                      ECal.OperationFlags.NONE, None)

    def delete(self, ev, all_occurrences=True):
        cal = self.by_uid(ev["cal"])
        if ev["rid"] and not all_occurrences:
            cal.client.remove_object_sync(ev["uid"], ev["rid"], ECal.ObjModType.THIS, ECal.OperationFlags.NONE, None)
        else:
            cal.client.remove_object_sync(ev["uid"], None, ECal.ObjModType.ALL, ECal.OperationFlags.NONE, None)
