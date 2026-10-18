"""notreal.contacts demo: made-up people with 555 numbers and example.com mail (no Evolution)."""
import book

NAMES = ["Anna Berg", "Ben Ahlström", "Carla Diaz", "Daniel Ek", "Elif Kaya", "Felix Grün", "Hanna Lind",
         "Ivan Petrov", "Julia Novak", "Kenji Sato", "Lea Park", "Marco Rossi", "Mum", "Nora Holm",
         "Oscar Wilde-Berg", "Priya Shah", "Sara Nyström", "Tom Ek", "Yusuf Demir"]
PEOPLE = []
for i, n in enumerate(NAMES):
    g, _, f = n.partition(" ")
    num = "+1555555%04d" % (100 + i)
    PEOPLE.append({"uid": "c%d" % i, "name": n, "given": g, "family": f, "org": "",
                   "tels": [{"value": num, "e164": num, "kind": "mobile"}],
                   "emails": ["%s@example.com" % g.lower()], "photo": None})
book.Book.connect = lambda self: "Contacts"
book.Book.load = lambda self: list(PEOPLE)
book.Book.cached = staticmethod(lambda: list(PEOPLE))
