import os, sys
os.environ["NOTAREAL_MAPS_SIM"] = "1"
os.environ["NOTAREAL_MAPS_KEY"] = "test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gmaps
gmaps.CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config-test.json")   # never touch the real recents/settings
OUT = sys.argv[1]
A=(59.3300,18.0600); B=(59.3408,18.0600); C=(59.3408,18.0705)
def line(a,b,n): return [(a[0]+(b[0]-a[0])*i/n, a[1]+(b[1]-a[1])*i/n) for i in range(n+1)]
PTS=line(A,B,30)+line(B,C,15)[1:]
class Fake(gmaps.Google):
    def tile_session(self, traffic=False): return "s"
    def tile_url(self, s): return "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    def autocomplete(self, text, near=None):
        return [{"id":"p1","name":"Test Café","sub":"Testgatan 1, Stockholm","meters":850}]
    def details(self, pid):
        return {"id":"p1","name":"Test Café","address":"Testgatan 1, 111 11 Stockholm","lat":C[0],"lng":C[1],
                "rating":4.5,"ratings":1203,"type":"Coffee shop","open":True,"hours":["Monday: 7–18"]*7,
                "phone":"08-31 61 10","web":"https://example.com","summary":"Busy third-wave café.","photos":[],
                "reviews":[{"who":"Anna","stars":5,"when":"a week ago","text":"Best cardamom bun in town."}]}
    def routes(self, origin, dest, mode="DRIVE", heading=None, alternatives=True):
        d1=gmaps.dist(A,B); d2=gmaps.dist(B,C)
        r={"pts":PTS,"mode":mode,"m":d1+d2,"s":240,"static_s":180,"via":"Sveavägen","labels":[],"warnings":[],
           "traffic":[(0,10,"NORMAL"),(10,25,"SLOW"),(25,45,"TRAFFIC_JAM")],
           "steps":[{"m":d1,"s":120,"maneuver":"DEPART","text":"Head north on Sveavägen","mode":mode,"transit":None,"pts":[]},
                    {"m":d2,"s":60,"maneuver":"TURN_RIGHT","text":"Turn right onto Odengatan","mode":mode,"transit":None,"pts":[]}]}
        alt=dict(r, pts=line(A,(A[0],C[1]),15)+line((A[0],C[1]),C,30)[1:], s=300, static_s=290, traffic=[], via="Birger Jarlsgatan")
        return [r, alt]
gmaps.Google = Fake
import maps as M
import voice
voice.Voice.say = lambda self, t: print("SAY:", t, flush=True)
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
    if t == 2: win._on_fix({"lat":A[0],"lng":A[1],"acc":8,"speed":0,"heading":None,"t":0})
    if t == 12:
        shot(win, "1-home"); win.entry.grab_focus(); win.entry.set_text("pascal")
    if t == 20:
        shot(win, "2-suggest"); win._pick(win.sugg.get_row_at_index(1).item)
    if t == 30: print("suggestions shown after pick:", win.sugg_rev.get_reveal_child()); shot(win, "3-place"); win.directions(win.place)
    if t == 40: shot(win, "4-routes"); win.start_nav(0)
    if t == 60: shot(win, "5-nav")
    if t == 62:
        win.follow = False; win.sim.stop()
        win._on_fix({"lat":B[0],"lng":B[1]+0.002,"acc":5,"speed":12,"heading":90.0,"t":0})
        win.follow = True; win._nav_camera(win.fix)
    if t == 66: shot(win, "5b-east")
    if t == 68: app.quit(); return False
    if t == 150:
        print("navigating:", win.navigating is not None); shot(win, "7-end"); app.quit(); return False
    return True
def poll():
    w = app.get_active_window()
    if w: GLib.timeout_add(250, step, w); return False
    return True
GLib.timeout_add(300, poll)
app.run([])
