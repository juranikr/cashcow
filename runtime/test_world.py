import unittest

from world import (
    COMPUTER_STATIONS,
    MEETING_SEATS,
    TOOL_SPOTS,
    distance,
    is_blocked,
    navigate_toward,
)


class WorldNavigationTests(unittest.TestCase):
    def walk(self, start, target):
        point = dict(start)
        facing = "south"
        stalled = 0
        for _ in range(800):
            if distance(point, target) <= 2.4:
                break
            next_point, facing = navigate_toward(point, target, 0.72, facing)
            self.assertLessEqual(distance(point, next_point), 0.721)
            self.assertFalse(is_blocked(next_point))
            stalled = stalled + 1 if distance(point, next_point) < 0.001 else 0
            self.assertLess(stalled, 12)
            point = next_point
        self.assertLessEqual(distance(point, target), 2.4)

    def test_all_rooms_are_reachable_from_all_agent_starts(self):
        starts = [
            {"x": 31, "y": 36}, {"x": 52, "y": 35},
            {"x": 64, "y": 60}, {"x": 44, "y": 69},
        ]
        destinations = [item["position"] for item in COMPUTER_STATIONS + MEETING_SEATS] + [TOOL_SPOTS["whiteboard"]]
        for start in starts:
            for destination in destinations:
                self.walk(start, destination)


if __name__ == "__main__":
    unittest.main()

