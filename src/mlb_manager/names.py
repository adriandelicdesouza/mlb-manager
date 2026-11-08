"""Fictional player name generation.

Names are random combinations of common first and last names; any resemblance
to a real player is coincidental.
"""

from __future__ import annotations

import random

FIRST_NAMES: tuple[str, ...] = (
    "Aaron", "Adrian", "Alejandro", "Alex", "Andres", "Andrew", "Angel", "Anthony",
    "Austin", "Ben", "Blake", "Bobby", "Brandon", "Brayden", "Brett", "Bryce",
    "Caleb", "Cameron", "Carlos", "Carter", "Chase", "Chris", "Cody", "Colby",
    "Colin", "Connor", "Corey", "Dalton", "Damian", "Daniel", "Dante", "Darius",
    "David", "Derek", "Diego", "Dominic", "Dylan", "Eduardo", "Eli", "Elijah",
    "Emilio", "Enrique", "Eric", "Ethan", "Evan", "Felix", "Fernando", "Francisco",
    "Gabriel", "Garrett", "Gavin", "Grant", "Gregory", "Hector", "Hunter", "Ian",
    "Isaac", "Ivan", "Jace", "Jack", "Jackson", "Jake", "Jalen", "James", "Jared",
    "Javier", "Jesus", "Joel", "Jonah", "Jordan", "Jorge", "Jose", "Joshua",
    "Juan", "Julian", "Justin", "Kai", "Keegan", "Kenji", "Kevin", "Kyle", "Landon",
    "Logan", "Lucas", "Luis", "Manuel", "Marco", "Marcus", "Mario", "Mason",
    "Mateo", "Matt", "Max", "Miguel", "Mitchell", "Nate", "Nathan", "Nick",
    "Noah", "Nolan", "Oliver", "Omar", "Oscar", "Owen", "Pablo", "Parker",
    "Patrick", "Pedro", "Rafael", "Ramon", "Randy", "Raul", "Reid", "Ricardo",
    "Riley", "Robert", "Roberto", "Ryan", "Ryota", "Sam", "Santiago", "Sean",
    "Sebastian", "Shane", "Shohei", "Spencer", "Taylor", "Trevor", "Tristan",
    "Tyler", "Victor", "Vince", "Wade", "Wesley", "Will", "Wyatt", "Xavier",
    "Yuki", "Zach", "Zane",
)

LAST_NAMES: tuple[str, ...] = (
    "Abbott", "Acosta", "Aguilar", "Alvarez", "Andrews", "Arias", "Bailey",
    "Baker", "Barnes", "Bauer", "Beck", "Bell", "Bennett", "Blake", "Bowman",
    "Boyd", "Bradley", "Brooks", "Bryant", "Burke", "Butler", "Caldwell",
    "Calhoun", "Campos", "Cardenas", "Carlson", "Carroll", "Castillo", "Castro",
    "Chambers", "Chavez", "Choi", "Clarke", "Cole", "Collins", "Contreras",
    "Cooper", "Cortes", "Crawford", "Cruz", "Cunningham", "Dawson", "Delgado",
    "Diaz", "Dixon", "Dominguez", "Duncan", "Dunn", "Ellis", "Espinoza", "Evans",
    "Farrell", "Fernandez", "Fields", "Fischer", "Fleming", "Flores", "Ford",
    "Foster", "Fowler", "Franco", "Fuentes", "Garcia", "Gardner", "Gibson",
    "Gomez", "Graham", "Grant", "Griffin", "Guerrero", "Gutierrez", "Hale",
    "Hansen", "Harper", "Hart", "Hayes", "Henderson", "Hernandez", "Hicks",
    "Hoffman", "Holland", "Hudson", "Ibarra", "Ito", "Jensen", "Jimenez",
    "Keller", "Kennedy", "Kim", "Klein", "Kowalski", "Lambert", "Lane", "Larsen",
    "Lawson", "Lee", "Leon", "Lopez", "Lowe", "Lucero", "Lynch", "Maldonado",
    "Marquez", "Marsh", "Martinez", "Matsuda", "Maxwell", "McBride", "McCoy",
    "Medina", "Mejia", "Mendoza", "Meyer", "Miller", "Mitchell", "Molina",
    "Montgomery", "Morales", "Moreno", "Murphy", "Nakamura", "Navarro", "Nelson",
    "Novak", "Nunez", "Ochoa", "O'Brien", "Olsen", "Ortega", "Ortiz", "Owens",
    "Pacheco", "Padilla", "Park", "Parsons", "Pena", "Perez", "Peterson", "Pierce",
    "Porter", "Price", "Quinn", "Ramirez", "Ramos", "Reed", "Reyes", "Rhodes",
    "Rios", "Rivera", "Robles", "Rodgers", "Rojas", "Rowe", "Ruiz", "Salazar",
    "Sanchez", "Santana", "Sato", "Schmidt", "Schultz", "Serrano", "Shaw",
    "Silva", "Simmons", "Soto", "Stanton", "Steele", "Stewart", "Suarez",
    "Sullivan", "Suzuki", "Tanaka", "Tate", "Torres", "Tran", "Tucker", "Valdez",
    "Vargas", "Vasquez", "Vaughn", "Vega", "Wagner", "Walsh", "Ward", "Warren",
    "Watts", "Weaver", "Webb", "Wheeler", "Whitaker", "Wolfe", "Yamamoto", "Young",
    "Zamora", "Zimmerman",
)


class NameGenerator:
    """Produces unique fictional full names from a seeded RNG."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._used: set[tuple[str, str]] = set()

    def next_name(self) -> tuple[str, str]:
        for _ in range(100):
            name = (self._rng.choice(FIRST_NAMES), self._rng.choice(LAST_NAMES))
            if name not in self._used:
                self._used.add(name)
                return name
        # Name space is ~27k combinations; fall back to allowing a repeat.
        return self._rng.choice(FIRST_NAMES), self._rng.choice(LAST_NAMES)
