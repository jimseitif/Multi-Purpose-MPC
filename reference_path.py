import numpy as np
import math
from map import Map
from skimage.draw import line
import matplotlib.pyplot as plt
import matplotlib.patches as plt_patches
from scipy import sparse
import osqp

# Colors
DRIVABLE_AREA = '#BDC3C7'
WAYPOINTS = '#D0D3D4'
OBSTACLE = '#2E4053'


############
# Waypoint #
############

class Waypoint:
    def __init__(self, x, y, psi, kappa):
        """
        Waypoint object containing x, y location in global coordinate system,
        orientation of waypoint psi and local curvature kappa
        :param x: x position in global coordinate system | [m]
        :param y: y position in global coordinate system | [m]
        :param psi: orientation of waypoint | [rad]
        :param kappa: local curvature | [1 / m]
        """
        self.x = x
        self.y = y
        self.psi = psi
        self.kappa = kappa

        # Reference velocity at this waypoint according to speed profile
        self.v_ref = None

        # Information about drivable area at waypoint
        # upper and lower bound of drivable area orthogonal to
        # waypoint orientation
        self.lb = None
        self.ub = None
        self.border_cells = None

    def __sub__(self, other):
        """
        Overload subtract operator. Difference of two waypoints is equal to
        their euclidean distance.
        :param other: subtrahend
        :return: euclidean distance between two waypoints
        """
        return ((self.x - other.x)**2 + (self.y - other.y)**2)**0.5


############
# Obstacle #
############

class Obstacle:
    def __init__(self, cx, cy, radius):
        """
        Constructor for a circular obstacle to be place on a path.
        :param cx: x coordinate of center of obstacle in world coordinates
        :param cy: y coordinate of center of obstacle in world coordinates
        :param radius: radius of circular obstacle in m
        """

        self.cx = cx
        self.cy = cy
        self.radius = radius

    def show(self):
        """
        Display obstacle.
        """

        # Draw circle
        circle = plt_patches.Circle(xy=(self.cx, self.cy), radius=
                                            self.radius, color=OBSTACLE)
        ax = plt.gca()
        ax.add_patch(circle)


##################
# Reference Path #
##################


class ReferencePath:
    def __init__(self, map, wp_x, wp_y, resolution, smoothing_distance,
                 max_width, circular):
        """
        Reference Path object. Create a reference trajectory from specified
        corner points with given resolution. Smoothing around corners can be
        applied. Waypoints represent center-line of the path with specified
        maximum width to both sides.
        :param map: map object on which path will be placed
        :param wp_x: x coordinates of corner points in global coordinates
        :param wp_y: y coordinates of corner points in global coordinates
        :param resolution: resolution of the path in m/wp
        :param smoothing_distance: number of waypoints used for smoothing the
        path by averaging neighborhood of waypoints
        :param max_width: maximum width of path to both sides in m
        :param circular: True if path circular
        """

        # Precision
        self.eps = 1e-12

        # Map
        self.map = map

        # Resolution of the path
        self.resolution = resolution

        # Look ahead distance for path averaging
        self.smoothing_distance = smoothing_distance

        # Circular flag
        self.circular = circular

        # List of waypoint objects
        self.waypoints = self._construct_path(wp_x, wp_y)

        # Number of waypoints
        self.n_waypoints = len(self.waypoints)

        # Length of path
        self.length, self.segment_lengths = self._compute_length()

        # Compute path width (attribute of each waypoint)
        self._compute_width(max_width=max_width)

        # Obstacles on path
        self.obstacles = list()

    def _construct_path(self, wp_x, wp_y):
        """
        Construct path from given waypoints.
        :param wp_x: x coordinates of waypoints in global coordinates
        :param wp_y: y coordinates of waypoints in global coordinates
        :return: list of waypoint objects
        """

        # Number of waypoints
        n_wp = [int(np.sqrt((wp_x[i + 1] - wp_x[i]) ** 2 +
                            (wp_y[i + 1] - wp_y[i]) ** 2) /
            self.resolution) for i in range(len(wp_x) - 1)]

        # Construct waypoints with specified resolution
        gp_x, gp_y = wp_x[-1], wp_y[-1]
        wp_x = [np.linspace(wp_x[i], wp_x[i+1], n_wp[i], endpoint=False).
                    tolist() for i in range(len(wp_x)-1)]
        wp_x = [wp for segment in wp_x for wp in segment] + [gp_x]
        wp_y = [np.linspace(wp_y[i], wp_y[i + 1], n_wp[i], endpoint=False).
                    tolist() for i in range(len(wp_y) - 1)]
        wp_y = [wp for segment in wp_y for wp in segment] + [gp_y]

        # Smooth path
        wp_xs = []
        wp_ys = []
        for wp_id in range(self.smoothing_distance, len(wp_x) -
                                                    self.smoothing_distance):
            wp_xs.append(np.mean(wp_x[wp_id - self.smoothing_distance:wp_id
                                            + self.smoothing_distance + 1]))
            wp_ys.append(np.mean(wp_y[wp_id - self.smoothing_distance:wp_id
                                            + self.smoothing_distance + 1]))

        # Construct list of waypoint objects
        waypoints = list(zip(wp_xs, wp_ys))
        waypoints = self._construct_waypoints(waypoints)

        return waypoints

    def _construct_waypoints(self, waypoint_coordinates):
        """
        Reformulate conventional waypoints (x, y) coordinates into waypoint
        objects containing (x, y, psi, kappa, ub, lb)
        :param waypoint_coordinates: list of (x, y) coordinates of waypoints in
        global coordinates
        :return: list of waypoint objects for entire reference path
        """

        # List containing waypoint objects
        waypoints = []

        # Iterate over all waypoints
        for wp_id in range(len(waypoint_coordinates) - 1):

            # Get start and goal waypoints
            current_wp = np.array(waypoint_coordinates[wp_id])
            next_wp = np.array(waypoint_coordinates[wp_id + 1])

            # Difference vector
            dif_ahead = next_wp - current_wp

            # Angle ahead
            psi = np.arctan2(dif_ahead[1], dif_ahead[0])

            # Distance to next waypoint
            dist_ahead = np.linalg.norm(dif_ahead, 2)

            # Get x and y coordinates of current waypoint
            x, y = current_wp[0], current_wp[1]

            # Compute local curvature at waypoint
            # first waypoint
            if wp_id == 0:
                kappa = 0
            else:
                prev_wp = np.array(waypoint_coordinates[wp_id - 1])
                dif_behind = current_wp - prev_wp
                angle_behind = np.arctan2(dif_behind[1], dif_behind[0])
                angle_dif = np.mod(psi - angle_behind + math.pi, 2 * math.pi) \
                            - math.pi
                kappa = angle_dif / (dist_ahead + self.eps)

            waypoints.append(Waypoint(x, y, psi, kappa))

        return waypoints

    def _compute_length(self):
        """
        Compute length of center-line path as sum of euclidean distance between
        waypoints.
        :return: length of center-line path in m
        """
        segment_lengths = [0.0] + [self.waypoints[wp_id+1] - self.waypoints
                    [wp_id] for wp_id in range(len(self.waypoints)-1)]
        s = sum(segment_lengths)
        return s, segment_lengths

    def _compute_width(self, max_width):
        """
        Compute the width of the path by checking the maximum free space to
        the left and right of the center-line.
        :param max_width: maximum width of the path.
        """

        # Iterate over all waypoints
        for wp_id, wp in enumerate(self.waypoints):
            # List containing information for current waypoint
            width_info = []
            # Check width left and right of the center-line
            for i, dir in enumerate(['left', 'right']):
                # Get angle orthogonal to path in current direction
                if dir == 'left':
                    angle = np.mod(wp.psi + math.pi / 2 + math.pi,
                                 2 * math.pi) - math.pi
                else:
                    angle = np.mod(wp.psi - math.pi / 2 + math.pi,
                                   2 * math.pi) - math.pi
                # Get closest cell to orthogonal vector
                t_x, t_y = self.map.w2m(wp.x + max_width * np.cos(angle), wp.y
                                        + max_width * np.sin(angle))
                # Compute distance to orthogonal cell on path border
                b_value, b_cell = self._get_min_width(wp, t_x, t_y, max_width)
                # Add information to list for current waypoint
                width_info.append(b_value)
                width_info.append(b_cell)

            # Set waypoint attributes with width to the left and right
            wp.ub = width_info[0]
            wp.lb = -1 * width_info[2]  # minus can be assumed as waypoints
            # represent center-line of the path
            # Set border cells of waypoint
            wp.border_cells = (width_info[1], width_info[3])

    def _get_min_width(self, wp, t_x, t_y, max_width):
        """
        Compute the minimum distance between the current waypoint and the
        orthogonal cell on the border of the path
        :param wp: current waypoint
        :param t_x: x coordinate of border cell in map coordinates
        :param t_y: y coordinate of border cell in map coordinates
        :param max_width: maximum path width in m
        :return: min_width to border and corresponding cell
        """

        # Get neighboring cells of orthogonal cell (account for
        # discretization inaccuracy)
        tn_x, tn_y = [], []
        for i in range(-1, 2, 1):
            for j in range(-1, 2, 1):
                tn_x.append(t_x+i)
                tn_y.append(t_y+j)

        # Get pixel coordinates of waypoint
        wp_x, wp_y = self.map.w2m(wp.x, wp.y)

        # Get Bresenham paths to all possible cells
        paths = []
        for t_x, t_y in zip(tn_x, tn_y):
            x_list, y_list = line(wp_x, wp_y, t_x, t_y)
            paths.append(zip(x_list, y_list))

        # Compute minimum distance to border cell
        min_width = max_width
        # map inspected cell to world coordinates
        min_cell = self.map.m2w(t_x, t_y)
        for path in paths:
            for cell in path:
                t_x, t_y = cell[0], cell[1]
                # If path goes through occupied cell
                if self.map.data[t_y, t_x] == 0:
                    # Get world coordinates
                    c_x, c_y = self.map.m2w(t_x, t_y)
                    cell_dist = np.sqrt((wp.x - c_x) ** 2 + (wp.y - c_y) ** 2)
                    if cell_dist < min_width:
                        min_width = cell_dist
                        min_cell = (c_x, c_y)

        return min_width, min_cell

    def compute_speed_profile(self, Constraints):
        """
        Compute a speed profile for the path. Assign a reference velocity
        to each waypoint based on its curvature.
        """

        # Set optimization horizon
        N = self.n_waypoints - 1

        # Constraints
        a_min = np.ones(N-1) * Constraints['a_min']
        a_max = np.ones(N-1) * Constraints['a_max']
        v_min = np.ones(N) * Constraints['v_min']
        v_max = np.ones(N) * Constraints['v_max']

        # Maximum lateral acceleration
        ay_max = Constraints['ay_max']

        # Inequality Matrix
        D1 = np.zeros((N-1, N))

        # Iterate over horizon
        for i in range(N):

            look_ahead = 30

            # Get information about current waypoint
            current_waypoint = self.get_waypoint(i)
            next_waypoint = self.get_waypoint(i+1)
            # distance between waypoints
            li = next_waypoint - current_waypoint
            # curvature of waypoint
            ki = current_waypoint.kappa
            if np.abs(ki) <= 0.1:
                kis = [wp.kappa for wp in self.waypoints[i:i+look_ahead]]
                ki = np.mean(kis)

            # Fill operator matrix
            # dynamics of acceleration
            if i < N-1:
                D1[i, i:i+2] = np.array([-1/(2*li), 1/(2*li)])

            # Compute dynamic constraint on velocity
            v_max_dyn = np.sqrt(ay_max / (np.abs(ki) + self.eps))
            if v_max_dyn < v_max[i]:
                v_max[i] = v_max_dyn

        # Construct inequality matrix
        D1 = sparse.csc_matrix(D1)
        D2 = sparse.eye(N)
        D = sparse.vstack([D1, D2], format='csc')

        # Get upper and lower bound vectors for inequality constraints
        l = np.hstack([a_min, v_min])
        u = np.hstack([a_max, v_max])

        # Set cost matrices
        P = sparse.eye(N, format='csc')
        q = -1 * v_max

        # Solve optimization problem
        problem = osqp.OSQP()
        problem.setup(P=P, q=q, A=D, l=l, u=u, verbose=False)
        speed_profile = problem.solve().x

        # Assign reference velocity to every waypoint
        for i, wp in enumerate(self.waypoints[:-1]):
            wp.v_ref = speed_profile[i]
        self.waypoints[-1].v_ref = self.waypoints[-2].v_ref

    def get_waypoint(self, wp_id):
        """
        Get waypoint corresponding to wp_id. Circular indexing supported.
        :param wp_id: unique waypoint ID
        :return: waypoint object
        """

        # Allow circular indexing if circular path
        if wp_id >= self.n_waypoints and self.circular:
            wp_id = np.mod(wp_id, self.n_waypoints)
        # Terminate execution if end of path reached
        elif wp_id >= self.n_waypoints and not self.circular:
            print('Reached end of path!')
            exit(1)

        return self.waypoints[wp_id]

    def update_bounds(self, wp_id, safety_margin):
        """
        Compute upper and lower bounds of the drivable area orthogonal to
        the given waypoint.
        :param safety_margin: safety margin of the car orthogonal to path in m
        :param wp_id: ID of reference waypoint
        """

        # Get reference waypoint
        wp = self.get_waypoint(wp_id)

        # Get waypoint's border cells in map coordinates
        ub_p = self.map.w2m(wp.border_cells[0][0], wp.border_cells[0][1])
        lb_p = self.map.w2m(wp.border_cells[1][0], wp.border_cells[1][1])

        # Compute path from left border cell to right border cell
        x_list, y_list = line(ub_p[0], ub_p[1], lb_p[0], lb_p[1])

        # Initialize upper and lower bound of drivable area to
        # upper bound of path
        ub_o, lb_o = ub_p, ub_p

        # Initialize upper and lower bound of best segment to upper bound of
        # path
        ub_ls, lb_ls = ub_p, ub_p

        # Iterate over path from left border to right border
        for x, y in zip(x_list[1:], y_list[1:]):
            # If cell is free, update lower bound
            if self.map.data[y, x] == 1:
                lb_o = (x, y)
            # If cell is occupied, end segment. Update best segment if current
            # segment is larger than previous best segment. Then, reset upper
            # and lower bound to current cell
            if self.map.data[y, x] == 0 or (x, y) == lb_p:
                if np.sqrt((ub_o[0]-lb_o[0])**2+(ub_o[1]-lb_o[1])**2) > \
                np.sqrt((ub_ls[0]-lb_ls[0])**2+(ub_ls[1]-lb_ls[1])**2):
                    ub_ls = ub_o
                    lb_ls = lb_o
                # Start new segment
                ub_o = (x, y)
                lb_o = (x, y)

        # Transform upper and lower bound cells to world coordinates
        ub_ls = self.map.m2w(ub_ls[0], ub_ls[1])
        lb_ls = self.map.m2w(lb_ls[0], lb_ls[1])

        # Check sign of upper and lower bound
        angle_ub = np.mod(np.arctan2(ub_ls[1] - wp.y, ub_ls[0] - wp.x)
                          - wp.psi + math.pi, 2*math.pi) - math.pi
        angle_lb = np.mod(np.arctan2(lb_ls[1] - wp.y, lb_ls[0] - wp.x)
                          - wp.psi + math.pi, 2*math.pi) - math.pi
        sign_ub = np.sign(angle_ub)
        sign_lb = np.sign(angle_lb)

        # Compute upper and lower bound of largest drivable area
        ub = sign_ub * np.sqrt((ub_ls[0]-wp.x)**2+(ub_ls[1]-wp.y)**2)
        lb = sign_lb * np.sqrt((lb_ls[0]-wp.x)**2+(lb_ls[1]-wp.y)**2)

        # Add safety margin (attribute of car) to bounds
        ub = ub - safety_margin
        lb = lb + safety_margin

        # Check feasibility of the path
        if ub < lb:
            # Upper and lower bound of 0 indicate an infeasible path
            # given the specified safety margin
            ub, lb = 0.0, 0.0

        # Compute absolute angle of bound cell
        angle_ub = np.mod(math.pi/2 + wp.psi + math.pi, 2 * math.pi) - math.pi
        angle_lb = np.mod(-math.pi/2 + wp.psi + math.pi, 2 * math.pi) - math.pi

        # Compute cell on bound for computed distance ub and lb
        ub_ls = wp.x + ub * np.cos(angle_ub), wp.y + ub * np.sin(angle_ub)
        lb_ls = wp.x - lb * np.cos(angle_lb), wp.y - lb * np.sin(angle_lb)
        border_cells = (ub_ls, lb_ls)

        return lb, ub, border_cells

    def add_obstacles(self, obstacles):
        """
        Add obstacles to the path.
        :param obstacles: list of obstacle objects
        """

        # Extend list of obstacles
        self.obstacles.extend(obstacles)

        # Iterate over list of obstacles
        for obstacle in obstacles:
            # Compute radius of circular object in pixels
            radius_px = int(np.ceil(obstacle.radius / self.map.resolution))
            # Get center coordinates of obstacle in map coordinates
            cx_px, cy_px = self.map.w2m(obstacle.cx, obstacle.cy)

            # Add circular object to map
            y, x = np.ogrid[-radius_px: radius_px, -radius_px: radius_px]
            index = x ** 2 + y ** 2 <= radius_px ** 2
            self.map.data[cy_px-radius_px:cy_px+radius_px, cx_px-radius_px:
                                                cx_px+radius_px][index] = 0

    def show(self, display_drivable_area=True):
        """
        Display path object on current figure.
        :param display_drivable_area: If True, display arrows indicating width
        of drivable area
        """

        # Clear figure
        plt.clf()

        # Disabled ticks
        plt.xticks([])
        plt.yticks([])

        # Plot map in gray-scale and set extent to match world coordinates
        #canvas = np.ones(self.map.data.shape)
        canvas = np.flipud(self.map.data)
        plt.imshow(canvas, cmap='gray',
                   extent=[self.map.origin[0], self.map.origin[0] +
                           self.map.width * self.map.resolution,
                           self.map.origin[1], self.map.origin[1] +
                           self.map.height * self.map.resolution], vmin=0.0,
                   vmax=1.0)

        # Get x and y coordinates for all waypoints
        wp_x = np.array([wp.x for wp in self.waypoints])
        wp_y = np.array([wp.y for wp in self.waypoints])

        # Get x and y locations of border cells for upper and lower bound
        wp_ub_x = np.array([wp.border_cells[0][0] for wp in self.waypoints])
        wp_ub_y = np.array([wp.border_cells[0][1] for wp in self.waypoints])
        wp_lb_x = np.array([wp.border_cells[1][0] for wp in self.waypoints])
        wp_lb_y = np.array([wp.border_cells[1][1] for wp in self.waypoints])

        # Plot waypoints
        plt.scatter(wp_x, wp_y, color=WAYPOINTS, s=10)

        # Plot arrows indicating drivable area
        if display_drivable_area:
            plt.quiver(wp_x, wp_y, wp_ub_x - wp_x, wp_ub_y - wp_y, scale=1,
                   units='xy', width=0.2*self.resolution, color=DRIVABLE_AREA,
                   headwidth=1, headlength=0)
            plt.quiver(wp_x, wp_y, wp_lb_x - wp_x, wp_lb_y - wp_y, scale=1,
                   units='xy', width=0.2*self.resolution, color=DRIVABLE_AREA,
                   headwidth=1, headlength=0)

        # Plot border of path
        bl_x = np.array([wp.border_cells[0][0] for wp in
                         self.waypoints] +
                        [self.waypoints[0].border_cells[0][0]])
        bl_y = np.array([wp.border_cells[0][1] for wp in
                         self.waypoints] +
                        [self.waypoints[0].border_cells[0][1]])
        br_x = np.array([wp.border_cells[1][0] for wp in
                         self.waypoints] +
                        [self.waypoints[0].border_cells[1][0]])
        br_y = np.array([wp.border_cells[1][1] for wp in
                         self.waypoints] +
                        [self.waypoints[0].border_cells[1][1]])

        # If circular path, connect start and end point
        if self.circular:
            plt.plot(bl_x, bl_y, color=OBSTACLE)
            plt.plot(br_x, br_y, color=OBSTACLE)
        # If not circular, close path at start and end
        else:
            plt.plot(bl_x[:-1], bl_y[:-1], color=OBSTACLE)
            plt.plot(br_x[:-1], br_y[:-1], color=OBSTACLE)
            plt.plot((bl_x[-2], br_x[-2]), (bl_y[-2], br_y[-2]), color=OBSTACLE)
            plt.plot((bl_x[0], br_x[0]), (bl_y[0], br_y[0]), color=OBSTACLE)

        # Plot obstacles
        for obstacle in self.obstacles:
            obstacle.show()

    def _compute_free_segments(self, wp, min_width):
        """
        Compute free path segments.
        :param wp: waypoint object
        :param min_width: minimum width of valid segment
        :return: segment candidates as list of tuples (ub_cell, lb_cell)
        """

        # Candidate segments
        free_segments = []

        # Get waypoint's border cells in map coordinates
        ub_p = self.map.w2m(wp.border_cells[0][0],
                            wp.border_cells[0][1])
        lb_p = self.map.w2m(wp.border_cells[1][0],
                            wp.border_cells[1][1])

        # Compute path from left border cell to right border cell
        x_list, y_list = line(ub_p[0], ub_p[1], lb_p[0], lb_p[1])

        # Initialize upper and lower bound of drivable area to
        # upper bound of path
        ub_o, lb_o = ub_p, ub_p

        # Assume occupied path
        free_cells = False

        # Iterate over path from left border to right border
        for x, y in zip(x_list[1:], y_list[1:]):
            # If cell is free, update lower bound
            if self.map.data[y, x] == 1:
                # Free cell detected
                free_cells = True
                lb_o = (x, y)
            # If cell is occupied or end of path, end segment. Add segment
            # to list of candidates. Then, reset upper and lower bound to
            # current cell.
            if (self.map.data[y, x] == 0 or (x, y) == lb_p) and free_cells:
                # Set lower bound to border cell of segment
                lb_o = (x, y)
                # Transform upper and lower bound cells to world coordinates
                ub_o = self.map.m2w(ub_o[0], ub_o[1])
                lb_o = self.map.m2w(lb_o[0], lb_o[1])
                # If segment larger than threshold, add to candidates
                if np.sqrt((ub_o[0]-lb_o[0])**2 + (ub_o[1]-lb_o[1])**2) > \
                    min_width:
                    free_segments.append((ub_o, lb_o))
                # Start new segment
                ub_o = (x, y)
                free_cells = False
            elif self.map.data[y, x] == 0 and not free_cells:
                ub_o = (x, y)
                lb_o = (x, y)

        return free_segments

    def update_path_constraints(self, wp_id, N, min_width, safety_margin):
        """
        Compute upper and lower bounds of the drivable area orthogonal to
        the given waypoint.
        """

        # container for constraints and border cells
        ub_hor = []
        lb_hor = []
        border_cells_hor = []

        # Iterate over horizon
        for n in range(N):

            # get corresponding waypoint
            wp = self.waypoints[wp_id+n]

            # Get list of free segments
            free_segments = self._compute_free_segments(wp, min_width)

            # First waypoint in horizon uses largest segment
            if n == 0:
                segment_lengths = [np.sqrt((seg[0][0]-seg[1][0])**2 +
                            (seg[0][1]-seg[1][1])**2) for seg in free_segments]
                ls_id = segment_lengths.index(max(segment_lengths))
                ub_ls, lb_ls = free_segments[ls_id]

            else:

                # Get border cells of selected segment at previous waypoint
                ub_pw, lb_pw = border_cells_hor[n-1]
                ub_pw, lb_pw = list(ub_pw), list(lb_pw)

                # Project border cells onto new waypoint in path direction
                wp_prev = self.waypoints[wp_id+n-1]
                delta_s = wp_prev - wp
                ub_pw[0] += delta_s * np.cos(wp_prev.psi)
                ub_pw[1] += delta_s * np.cos(wp_prev.psi)
                lb_pw[0] += delta_s * np.sin(wp_prev.psi)
                lb_pw[1] += delta_s * np.sin(wp_prev.psi)

                # Iterate over free segments for current waypoint
                if len(free_segments) >= 2:

                    # container for overlap of segments with projection
                    segment_offsets = []

                    for free_segment in free_segments:

                        # Get border cells of segment
                        ub_fs, lb_fs = free_segment

                        # distance between upper bounds and lower bounds
                        d_ub = np.sqrt((ub_fs[0]-ub_pw[0])**2 + (ub_fs[1]-ub_pw[1])**2)
                        d_lb = np.sqrt((lb_fs[0]-lb_pw[0])**2 + (lb_fs[1]-lb_pw[1])**2)
                        mean_dist = (d_ub + d_lb) / 2

                        # Append offset to projected previous segment
                        segment_offsets.append(mean_dist)

                    # Select segment with minimum offset to projected previous
                    # segment
                    ls_id = segment_offsets.index(min(segment_offsets))
                    ub_ls, lb_ls = free_segments[ls_id]

                # Select free segment in case of only one candidate
                elif len(free_segments) == 1:
                    ub_ls, lb_ls = free_segments[0]

                # Set waypoint coordinates as bound cells if no feasible
                # segment available
                else:
                    ub_ls, lb_ls = (wp.x, wp.y), (wp.x, wp.y)

            # Check sign of upper and lower bound
            angle_ub = np.mod(np.arctan2(ub_ls[1] - wp.y, ub_ls[0] - wp.x)
                                  - wp.psi + math.pi, 2 * math.pi) - math.pi
            angle_lb = np.mod(np.arctan2(lb_ls[1] - wp.y, lb_ls[0] - wp.x)
                                  - wp.psi + math.pi, 2 * math.pi) - math.pi
            sign_ub = np.sign(angle_ub)
            sign_lb = np.sign(angle_lb)

            # Compute upper and lower bound of largest drivable area
            ub = sign_ub * np.sqrt(
                    (ub_ls[0] - wp.x) ** 2 + (ub_ls[1] - wp.y) ** 2)
            lb = sign_lb * np.sqrt(
                    (lb_ls[0] - wp.x) ** 2 + (lb_ls[1] - wp.y) ** 2)

            # Subtract safety margin
            ub -= safety_margin
            lb += safety_margin

            # Check feasibility of the path
            if ub < lb:
                # Upper and lower bound of 0 indicate an infeasible path
                # given the specified safety margin
                ub, lb = 0.0, 0.0

            # Compute absolute angle of bound cell
            angle_ub = np.mod(math.pi / 2 + wp.psi + math.pi,
                                  2 * math.pi) - math.pi
            angle_lb = np.mod(-math.pi / 2 + wp.psi + math.pi,
                                  2 * math.pi) - math.pi
            # Compute cell on bound for computed distance ub and lb
            ub_ls = wp.x + ub * np.cos(angle_ub), wp.y + ub * np.sin(
                    angle_ub)
            lb_ls = wp.x - lb * np.cos(angle_lb), wp.y - lb * np.sin(
                    angle_lb)
            bound_cells = (ub_ls, lb_ls)

            # Append results
            ub_hor.append(ub)
            lb_hor.append(lb)
            border_cells_hor.append(list(bound_cells))

        return np.array(ub_hor), np.array(lb_hor), border_cells_hor


if __name__ == '__main__':

    # Select Path | 'Race' or 'Q'
    path = 'Q'

    # Create Map
    if path == 'Race':
        map = Map(file_path='map_race.png', origin=[-1, -2], resolution=0.005)
        # Specify waypoints
        wp_x = [-0.75, -0.25, -0.25, 0.25, 0.25, 1.25, 1.25, 0.75, 0.75, 1.25,
                1.25, -0.75, -0.75, -0.25]
        wp_y = [-1.5, -1.5, -0.5, -0.5, -1.5, -1.5, -1, -1, -0.5, -0.5, 0, 0,
                -1.5, -1.5]
        # Specify path resolution
        path_resolution = 0.05  # m / wp
        reference_path = ReferencePath(map, wp_x, wp_y, path_resolution,
                     smoothing_distance=5, max_width=0.15,
                                       circular=True)
        # Add obstacles
        obs1 = Obstacle(cx=0.0, cy=0.0, radius=0.05)
        obs2 = Obstacle(cx=-0.8, cy=-0.5, radius=0.05)
        obs3 = Obstacle(cx=-0.7, cy=-1.5, radius=0.05)
        obs4 = Obstacle(cx=-0.3, cy=-1.0, radius=0.05)
        obs5 = Obstacle(cx=0.3, cy=-1.0, radius=0.05)
        obs6 = Obstacle(cx=0.75, cy=-1.5, radius=0.05)
        obs7 = Obstacle(cx=0.7, cy=-0.9, radius=0.05)
        obs8 = Obstacle(cx=1.2, cy=0.0, radius=0.05)
        reference_path.add_obstacles([obs1, obs2, obs3, obs4, obs5, obs6, obs7,
                                      obs8])

    elif path == 'Q':
        map = Map(file_path='map_floor2.png')
        wp_x = [-9.169, 11.9, 7.3, -6.95]
        wp_y = [-15.678, 10.9, 14.5, -3.31]
        # Specify path resolution
        path_resolution = 0.20  # m / wp
        reference_path = ReferencePath(map, wp_x, wp_y, path_resolution,
                                       smoothing_distance=5, max_width=1.5,
                                    circular=False)
        obs1 = Obstacle(cx=-6.3, cy=-11.1, radius=0.20)
        obs2 = Obstacle(cx=-2.2, cy=-6.8, radius=0.25)
        obs3 = Obstacle(cx=1.7, cy=-1.0, radius=0.15)
        obs4 = Obstacle(cx=2.0, cy=-1.2, radius=0.25)
        obs5 = Obstacle(cx=2.2, cy=-0.86, radius=0.1)
        obs6 = Obstacle(cx=2.33, cy=-0.7, radius=0.1)
        obs7 = Obstacle(cx=2.67, cy=-0.73, radius=0.1)
        obs8 = Obstacle(cx=6.42, cy=3.97, radius=0.3)
        obs9 = Obstacle(cx=7.42, cy=4.97, radius=0.3)
        obs10 = Obstacle(cx=7.14, cy=5.7, radius=0.1)
        reference_path.add_obstacles([obs1, obs2, obs3, obs4, obs5, obs6, obs7, obs8, obs9, obs10])

    else:
        reference_path = None
        print('Invalid path!')
        exit(1)

    ub, lb, border_cells = reference_path.update_path_constraints(0, reference_path.n_waypoints, 0.60, 0.1)
    # Get x and y locations of border cells for upper and lower bound
    for wp_id in range(reference_path.n_waypoints):
        if ub[wp_id] > 0.0 and lb[wp_id] > 0.0:
            print(wp_id)
        reference_path.waypoints[wp_id].border_cells = border_cells[wp_id]
    reference_path.show()

    plt.show()



