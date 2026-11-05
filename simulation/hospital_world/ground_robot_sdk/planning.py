"""Occupancy-grid costmaps and deterministic A* path planning.

The planner is middleware independent.  Coordinates are expressed in the
same startup-local metric frame used by :class:`NavigationController`.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from typing import Iterable, Mapping, Sequence


Cell = tuple[int, int]
Point = tuple[float, float]


@dataclass(frozen=True)
class CostmapConfig:
    resolution: float = 0.20
    corridor_width: float = 2.0
    robot_radius: float = 0.28
    margin: float = 1.0
    junction_radius: float = 1.5


class OccupancyGrid:
    """A compact binary costmap with metric/cell conversions."""

    def __init__(self, width: int, height: int, resolution: float, origin_x: float, origin_y: float, occupied: bool = False):
        if width <= 0 or height <= 0 or resolution <= 0:
            raise ValueError("costmap dimensions and resolution must be positive")
        self.width=int(width); self.height=int(height); self.resolution=float(resolution)
        self.origin_x=float(origin_x); self.origin_y=float(origin_y)
        self._occupied=bytearray([1 if occupied else 0])*(self.width*self.height)

    def copy(self) -> "OccupancyGrid":
        result=OccupancyGrid(self.width,self.height,self.resolution,self.origin_x,self.origin_y)
        result._occupied=bytearray(self._occupied)
        return result

    def contains_cell(self, cell: Cell) -> bool:
        return 0 <= cell[0] < self.width and 0 <= cell[1] < self.height

    def contains_point(self, point: Point) -> bool:
        return self.contains_cell(self.world_to_cell(*point))

    def world_to_cell(self, x: float, y: float) -> Cell:
        return int(math.floor((float(x)-self.origin_x)/self.resolution)),int(math.floor((float(y)-self.origin_y)/self.resolution))

    def cell_to_world(self, cell: Cell) -> Point:
        return self.origin_x+(cell[0]+.5)*self.resolution,self.origin_y+(cell[1]+.5)*self.resolution

    def is_occupied(self, cell: Cell, dynamic: set[Cell] | None = None) -> bool:
        return not self.contains_cell(cell) or bool(self._occupied[cell[1]*self.width+cell[0]]) or bool(dynamic and cell in dynamic)

    def set_occupied(self, cell: Cell, occupied: bool = True) -> None:
        if self.contains_cell(cell): self._occupied[cell[1]*self.width+cell[0]]=1 if occupied else 0

    def paint_disk(self, point: Point, radius: float, occupied: bool) -> None:
        center=self.world_to_cell(*point); cells=max(0,int(math.ceil(radius/self.resolution)))
        for dx in range(-cells,cells+1):
            for dy in range(-cells,cells+1):
                cell=(center[0]+dx,center[1]+dy)
                wx,wy=self.cell_to_world(cell)
                if math.hypot(wx-point[0],wy-point[1]) <= radius: self.set_occupied(cell,occupied)

    def paint_segment(self, start: Point, end: Point, radius: float, occupied: bool) -> None:
        length=math.hypot(end[0]-start[0],end[1]-start[1])
        steps=max(1,int(math.ceil(length/(self.resolution*.5))))
        for index in range(steps+1):
            ratio=index/steps
            self.paint_disk((start[0]+ratio*(end[0]-start[0]),start[1]+ratio*(end[1]-start[1])),radius,occupied)

    def inflated_cells(self, points: Iterable[Point], radius: float) -> set[Cell]:
        blocked:set[Cell]=set(); cells=max(0,int(math.ceil(radius/self.resolution)))
        for point in points:
            center=self.world_to_cell(*point)
            for dx in range(-cells,cells+1):
                for dy in range(-cells,cells+1):
                    cell=(center[0]+dx,center[1]+dy)
                    if not self.contains_cell(cell): continue
                    wx,wy=self.cell_to_world(cell)
                    if math.hypot(wx-point[0],wy-point[1]) <= radius: blocked.add(cell)
        return blocked

    @classmethod
    def from_corridor_graph(
        cls,
        waypoints: Mapping[str, Sequence[float]],
        edges: Iterable[Sequence[object]],
        config: CostmapConfig = CostmapConfig(),
    ) -> "OccupancyGrid":
        if not waypoints: raise ValueError("at least one waypoint is required")
        points={name:(float(value[0]),float(value[1])) for name,value in waypoints.items()}
        xs=[point[0] for point in points.values()]; ys=[point[1] for point in points.values()]
        padding=config.margin+config.corridor_width/2
        origin_x=min(xs)-padding; origin_y=min(ys)-padding
        width=int(math.ceil((max(xs)-min(xs)+2*padding)/config.resolution))+1
        height=int(math.ceil((max(ys)-min(ys)+2*padding)/config.resolution))+1
        grid=cls(width,height,config.resolution,origin_x,origin_y,occupied=True)
        traversable_radius=max(config.resolution,config.corridor_width/2-config.robot_radius)
        junction_radius=max(traversable_radius,config.junction_radius-config.robot_radius)
        for point in points.values(): grid.paint_disk(point,junction_radius,False)
        for edge in edges:
            if len(edge) < 2 or str(edge[0]) not in points or str(edge[1]) not in points: continue
            grid.paint_segment(points[str(edge[0])],points[str(edge[1])],traversable_radius,False)
        return grid


class AStarPlanner:
    """Eight-connected A* with corner-cut prevention and path simplification."""

    _NEIGHBOURS=((1,0,1.),(-1,0,1.),(0,1,1.),(0,-1,1.),(1,1,math.sqrt(2)),(1,-1,math.sqrt(2)),(-1,1,math.sqrt(2)),(-1,-1,math.sqrt(2)))

    def __init__(self, grid: OccupancyGrid): self.grid=grid

    def plan(self, start: Point, goal: Point, dynamic: set[Cell] | None = None) -> list[Point] | None:
        if not self.grid.contains_point(start) or not self.grid.contains_point(goal): return None
        start_cell=self._nearest_free(self.grid.world_to_cell(*start),dynamic)
        goal_cell=self._nearest_free(self.grid.world_to_cell(*goal),dynamic)
        if start_cell is None or goal_cell is None: return None
        queue:list[tuple[float,float,Cell]]=[(self._heuristic(start_cell,goal_cell),0.,start_cell)]
        costs={start_cell:0.}; parents:dict[Cell,Cell]={}; visited:set[Cell]=set()
        while queue:
            _,cost,current=heapq.heappop(queue)
            if current in visited: continue
            visited.add(current)
            if current == goal_cell:
                cells=self._reconstruct(parents,current)
                cells=self._simplify(cells,dynamic)
                path=[start]
                path.extend(self.grid.cell_to_world(cell) for cell in cells[1:-1])
                path.append(goal)
                return self._densify(path,max(self.grid.resolution*1.5,.25))
            for dx,dy,step in self._NEIGHBOURS:
                neighbour=(current[0]+dx,current[1]+dy)
                if self.grid.is_occupied(neighbour,dynamic): continue
                if dx and dy and (self.grid.is_occupied((current[0]+dx,current[1]),dynamic) or self.grid.is_occupied((current[0],current[1]+dy),dynamic)): continue
                candidate=cost+step
                if candidate >= costs.get(neighbour,float("inf")): continue
                costs[neighbour]=candidate; parents[neighbour]=current
                heapq.heappush(queue,(candidate+self._heuristic(neighbour,goal_cell),candidate,neighbour))
        return None

    def _nearest_free(self, cell: Cell, dynamic: set[Cell] | None) -> Cell | None:
        if not self.grid.is_occupied(cell,dynamic): return cell
        for radius in range(1,6):
            candidates=[]
            for dx in range(-radius,radius+1):
                for dy in (-radius,radius): candidates.append((cell[0]+dx,cell[1]+dy))
            for dy in range(-radius+1,radius):
                for dx in (-radius,radius): candidates.append((cell[0]+dx,cell[1]+dy))
            for candidate in candidates:
                if not self.grid.is_occupied(candidate,dynamic): return candidate
        return None

    @staticmethod
    def _heuristic(left: Cell, right: Cell) -> float:
        dx=abs(left[0]-right[0]); dy=abs(left[1]-right[1])
        return max(dx,dy)+(math.sqrt(2)-1)*min(dx,dy)

    @staticmethod
    def _reconstruct(parents: dict[Cell,Cell], current: Cell) -> list[Cell]:
        result=[current]
        while current in parents: current=parents[current]; result.append(current)
        result.reverse(); return result

    def _line_free(self, start: Cell, end: Cell, dynamic: set[Cell] | None) -> bool:
        x0,y0=start; x1,y1=end; dx=abs(x1-x0); dy=abs(y1-y0); sx=1 if x0<x1 else -1; sy=1 if y0<y1 else -1; error=dx-dy
        previous=(x0,y0)
        while True:
            current=(x0,y0)
            if self.grid.is_occupied(current,dynamic): return False
            if current != previous and current[0] != previous[0] and current[1] != previous[1]:
                if self.grid.is_occupied((current[0],previous[1]),dynamic) or self.grid.is_occupied((previous[0],current[1]),dynamic): return False
            if current == (x1,y1): return True
            previous=current; twice=2*error
            if twice > -dy: error-=dy; x0+=sx
            if twice < dx: error+=dx; y0+=sy

    def _simplify(self, cells: list[Cell], dynamic: set[Cell] | None) -> list[Cell]:
        if len(cells) <= 2: return cells
        result=[cells[0]]; anchor=0
        while anchor < len(cells)-1:
            candidate=len(cells)-1
            while candidate > anchor+1 and not self._line_free(cells[anchor],cells[candidate],dynamic): candidate-=1
            result.append(cells[candidate]); anchor=candidate
        return result

    @staticmethod
    def _densify(path: list[Point], spacing: float) -> list[Point]:
        if len(path) < 2: return path
        result=[path[0]]
        for start,end in zip(path,path[1:]):
            length=math.hypot(end[0]-start[0],end[1]-start[1])
            steps=max(1,int(math.ceil(length/spacing)))
            for index in range(1,steps+1):
                ratio=index/steps
                result.append((start[0]+ratio*(end[0]-start[0]),start[1]+ratio*(end[1]-start[1])))
        return result


def path_length(path: Sequence[Point]) -> float:
    return sum(math.hypot(right[0]-left[0],right[1]-left[1]) for left,right in zip(path,path[1:]))
