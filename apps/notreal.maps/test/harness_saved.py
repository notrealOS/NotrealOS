import io, json, os, sys, zipfile
os.environ["NOTAREAL_MAPS_SIM"] = "1"
os.environ["NOTAREAL_MAPS_KEY"] = "test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT, TMP = sys.argv[1], sys.argv[2]
import gmaps, saved
saved.DATA = TMP; saved.STORE = os.path.join(TMP, "saved.json")
if os.path.exists(saved.STORE): os.remove(saved.STORE)
# fake takeout
zp = os.path.join(TMP, "takeout-test.zip")
with zipfile.ZipFile(zp, "w") as z:
    z.writestr("Takeout/Saved/Want to go.csv", "﻿Title,Note,URL,Tags,Comment\n"
        "Test Café,cardamom buns,https://www.google.com/maps/place/Caf%C3%A9+Test Café/data=!4m2!3m1!1s0x465f9d6a:0x1a2b,,\n"
        "Fotografiska,,https://www.google.com/maps/place/Fotografiska/data=!4m2!3m1!1s0x465f77e1:0xff,,\n"
        ",,https://www.google.com/maps/search/59.31234,18.07654,,\n"
        "Nowhere Bar,,https://www.google.com/maps/place/Nowhere/data=!4m2!3m1!1s0x1:0x99,,\n")
    z.writestr("Takeout/Saved/Favourite places.csv", "Title,Note,URL\nGustavsberg Porslin,,https://www.google.com/maps/place/x/data=!3d59.3256!4d18.3889\n")
    z.writestr("Takeout/Maps (your places)/Saved Places.json", json.dumps({"type":"FeatureCollection","features":[
        {"geometry":{"type":"Point","coordinates":[18.0711,59.3251]},"properties":{"google_maps_url":"http://maps.google.com/?cid=777",
         "location":{"name":"Kungliga slottet","address":"Slottsbacken 1, Stockholm"}}}]}))
    z.writestr("Takeout/Maps (your places)/Reviews.json", json.dumps({"type":"FeatureCollection","features":[]}))
calls = []
class Fake(gmaps.Google):
    def tile_session(self, traffic=False): return "s"
    def tile_url(self, s): return "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    def search_cid(self, q, near=None):
        calls.append((q, near))
        db = {"Test Café": [{"id":"wrong","name":"Test Café Annex","address":"elsewhere","lat":59.0,"lng":18.0,"cid":"1"},
                              {"id":"p1","name":"Test Café","address":"Testgatan 1","lat":59.3408,"lng":18.0560,"cid":str(0x1a2b)}],
              "Fotografiska": [{"id":"p2","name":"Fotografiska","address":"Stadsgårdshamnen 22","lat":59.3178,"lng":18.0857,"cid":"5"}]}
        return db.get(q, [])
    def details(self, pid):
        return {"id":pid,"name":"Test Café","address":"Testgatan 1","lat":59.3408,"lng":18.0560,"hours":[],"photos":[],"reviews":[]}
gmaps.Google = Fake
import maps as M, voice
voice.Voice.say = lambda self, t: None
voice.Voice.prepare = lambda self, t: None
from gi.repository import GLib, Gtk, Graphene
M.APP_ID = "dev.notareal.MapsTest"
app = M.App()
def shot(win, name):
    p = Gtk.WidgetPaintable.new(win); w, h = win.get_width(), win.get_height()
    snap = Gtk.Snapshot(); p.snapshot(snap, w, h); node = snap.to_node()
    if node: win.get_renderer().render_texture(node, Graphene.Rect().init(0,0,w,h)).save_to_png(f"{OUT}/{name}.png")
def step(win, n=[0]):
    n[0] += 1; t = n[0]
    if t == 2: win._on_fix({"lat":59.33,"lng":18.06,"acc":8,"speed":0,"heading":None,"t":0})
    if t == 6: win.show_saved(); shot(win, "s0-empty") if False else None
    if t == 10: shot(win, "s1-empty"); win.import_takeout(zp)
    if t == 24:
        d = saved.load()
        print("lists:", {k: [(p["name"], p.get("approx"), round(p["lat"],4)) for p in v] for k, v in d["lists"].items()})
        print("lookups:", calls)
        print("markers:", len(win.saved_layer.get_markers()))
        shot(win, "s2-saved")
    if t == 28:
        win.viewport.set_zoom_level(12); win.map.center_on(59.33, 18.07)
    if t == 34: shot(win, "s3-map"); win.voice_navigate("fotografiska")
    if t == 44:
        print("navigate to saved -> dest:", win.dest and win.dest.get("name"), "routes:", len(win.routes))
        win.end_directions()
        win.show_place({"id":"p9","name":"Test Place","address":"Somewhere 1","lat":59.335,"lng":18.05})
    if t == 50:
        shot(win, "s4-card")
        star = [b for b in win.panel_body.observe_children() if False]
        win._toggle_saved(win.place, Gtk.Button(label="☆"))
        print("saved after star:", saved.is_saved(win.place), "default list:", [p["name"] for p in saved.load()["lists"].get("Saved", [])])
        print("reimport added:", saved.import_takeout(zp, win.g, None)["added"])
    if t == 56: app.quit(); return False
    return True
def poll():
    w = app.get_active_window()
    if w: GLib.timeout_add(250, step, w); return False
    return True
GLib.timeout_add(300, poll)
app.run([])
