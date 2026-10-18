# transit route: walk (dashed blue) -> bus 4 (line color) -> walk; screenshot shots/walk/
import os, sys
os.environ["NOTAREAL_MAPS_SIM"] = "1"
os.environ["NOTAREAL_MAPS_KEY"] = "test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gmaps
gmaps.CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config-test.json")   # never touch the real recents/settings
OUT = sys.argv[1]
A=(59.3300,18.0600); B=(59.3330,18.0600); C=(59.3330,18.0760); D=(59.3365,18.0760)
def line(a,b,n): return [(a[0]+(b[0]-a[0])*i/n, a[1]+(b[1]-a[1])*i/n) for i in range(n+1)]
class Fake(gmaps.Google):
    def tile_session(self, traffic=False): return "s"
    def tile_url(self, s): return "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    def routes(self, origin, dest, mode="DRIVE", heading=None, alternatives=True):
        w1, bus, w2 = line(A,B,10), line(B,C,20), line(C,D,10)
        tr = {"line":"4","vehicle":"Bus","color":"#e3000f","headsign":"Radiohuset","from":"Odenplan","to":"Fridhemsplan","dep":"12:50","stops":5}
        steps = [{"m":330,"s":240,"maneuver":"DEPART","text":"Walk","mode":"WALK","transit":None,"pts":w1},
                 {"m":900,"s":300,"maneuver":"STRAIGHT","text":"Bus 4","mode":"TRANSIT","transit":tr,"pts":bus},
                 {"m":390,"s":280,"maneuver":"STRAIGHT","text":"Walk","mode":"WALK","transit":None,"pts":w2}]
        return [{"pts":w1+bus[1:]+w2[1:],"mode":mode,"m":1620,"s":820,"static_s":820,"via":"","labels":[],
                 "warnings":[],"traffic":[],"steps":steps}]
gmaps.Google = Fake
import maps as M
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
    if t == 6:
        win.place = {"id":"p","name":"Test","address":"x","lat":D[0],"lng":D[1]}
        win.mode = "TRANSIT"
        win.directions(win.place)
    if t == 30:
        print("layers:", [(l.get_stroke_color().to_string(), list(l.get_dash() or [])) for l in win.route_layers], flush=True)
        shot(win, "transit"); app.quit(); return False
    return True
def poll():
    w = app.get_active_window()
    if w: GLib.timeout_add(250, step, w); return False
    return True
GLib.timeout_add(300, poll)
app.run([])
