from typing import List, Dict, Optional, Iterable
from itertools import chain
from functools import partial
from multiprocessing import Manager, Pool, Queue, cpu_count

import numpy as np
import simpy
from copy import copy
from . import simulation as cs
from .cache import get_from_cache, save_to_cache
from .lab import laboratory
from .parameters import Parameters
from .population import Population
from .progress import ProgressBar
from .stats import Stats
from .simulation_environment import SimulationEnvironment, SimulationRandomness
from .metrics import METRICS

SIMULATION_ENGINE_VERSION = '0.0.2'


def get_stats_matrix(populations: Dict, duration):
    num_populations = len(populations)
    num_metrics = len(cs.MEASUREMENTS)
    num_ages = len(cs.age_str)
    stats = np.zeros([num_populations, num_metrics, num_ages, duration])
    return stats


def track_population(senv: SimulationEnvironment):
    while True:
        yield senv.env.timeout(1.0)
        if senv.d0 is None:
            if senv.sim_params.d0_infections * senv.scaling < np.array([p.infected for p in senv.people]).sum():
                senv.d0 = int(senv.env.now + 0.01)
            else:
                continue
        if int(senv.env.now + 0.01) - senv.d0 >= senv.duration:
            return
        cs.log_stats(senv)
        if senv.simulation_queue:
            senv.simulation_queue.put(1)


def get_house_size(house_sizes):  # Number of people living in the same house
    return cs.p_choice(house_sizes)


def get_age_group(age_probabilities, age_risk):
    return age_risk[cs.p_choice(age_probabilities)]


def set_initial_infection(sim_params: Parameters, people: Iterable[cs.Person]):
    success = False
    while not success:
        someone = cs.choice(people, 1)[0]
        if someone.age_group.index < sim_params.min_age_group_initially_infected:
            continue
        success = someone.expose_to_virus()


def get_population(senv: SimulationEnvironment, population_params: Population) -> \
        List:
    people = []
    n = int(population_params.inhabitants * senv.scaling)
    initially_infected = population_params.seed_infections
    while len(people) < n:
        people.extend(generate_people_in_new_house(senv, population_params))
    for _ in range(initially_infected):
        set_initial_infection(senv.sim_params, people)
    if senv.creation_queue:
        senv.creation_queue.put(len(people))
    return people


def generate_people_in_new_house(senv: SimulationEnvironment, population_params: Population):
    house_size = get_house_size(population_params.home_size_probabilities)
    house = cs.Home(population_params.geosocial_displacement)
    age_probabilities = population_params.age_probabilities
    age_groups = population_params.age_groups
    age_group_house = get_age_group(age_probabilities, age_groups)
    home_age_cofactor = senv.sim_params.home_age_cofactor
    for _ in range(house_size):
        age_group = (age_group_house
                     if np.random.random() < home_age_cofactor
                     else get_age_group(age_probabilities, age_groups)
                     )
        yield cs.Person(senv, age_group, house)


def create_populations(senv: SimulationEnvironment) -> Dict[str, List[cs.Person]]:
    populations = {}
    for population_params in senv.sim_params.population_segments:
        for i, age_group in enumerate(population_params.age_groups):
            age_group_cp = copy(age_group)
            severity = np.array(age_group_cp.severity)
            age_bias = senv.randomness.severity_bias * (i - 4)
            new_odds = np.exp(np.log(severity / (1.0 - severity)
                                     ) - senv.randomness.severity_deviation + age_bias)
            age_group_cp.severity = new_odds / (1.0 + new_odds)
            population_params.age_groups[i] = age_group_cp
        populations[population_params.name] = get_population(senv, population_params)
    return populations


def simulate(
        sim_number,
        sim_params,
        simulation_size,
        duration,
        simulate_capacity,
        add_noise,
        use_cache,
        creation_queue: Optional[Queue] = None,
        simulation_queue: Optional[Queue] = None,
):
    if use_cache:
        args = (
            sim_number, sim_params, simulation_size, duration, simulate_capacity, add_noise, SIMULATION_ENGINE_VERSION)
        results = get_from_cache(args)
        if results:
            if creation_queue:
                creation_queue.put(simulation_size)
            if simulation_queue:
                simulation_queue.put(duration)
            return results[1]

    cs.seed(sim_number)
    np.random.seed(sim_number)
    scaling = simulation_size / sim_params.total_inhabitants
    env = simpy.Environment()
    sim_params = sim_params.clone()
    randomness = get_simulation_randomness(sim_params)
    senv = SimulationEnvironment(
        env=env,
        sim_params=sim_params,
        duration=duration,
        sim_number=sim_number,
        scaling=scaling,
        simulate_capacity=simulate_capacity,
        isolation_factor=0.0,
        randomness=randomness,
        attention=simpy.resources.resource.PriorityResource(env,
                                                            capacity=int(sim_params.capacity_hospital_max * scaling)),
        hospital_bed=simpy.resources.resource.PriorityResource(env,
                                                               capacity=int(
                                                                   sim_params.capacity_hospital_beds * scaling)),
        ventilator=simpy.resources.resource.PriorityResource(env,
                                                             capacity=int(sim_params.capacity_ventilators * scaling)),
        icu=simpy.resources.resource.PriorityResource(env, capacity=int(sim_params.capacity_icu * scaling)),
        stats=get_stats_matrix(sim_params.population_segments, duration),
        creation_queue=creation_queue,
        simulation_queue=simulation_queue,
        lab=laboratory(env, scaling),
    )
    senv.populations = create_populations(senv)
    senv.people = list(chain.from_iterable(senv.populations.values()))
    env.process(track_population(senv))
    for intervention in sim_params.interventions:
        intervention.setup(senv)
    while not senv.d0:
        env.run(until=env.now + 1)
    env.run(until=duration + senv.d0 + 0.011)
    stats = senv.stats / senv.scaling
    if use_cache:
        save_to_cache(args, stats)
    return stats


def get_simulation_randomness(sim_params: Parameters):
    return SimulationRandomness(
        severity_deviation=(np.random.random() + np.random.random() - 1.0) * 0.2,
        severity_bias=(np.random.random() - 0.5) * 0.2,
        isolation_deviation=np.random.random(),  # uncertainty regarding isolation effectiveness
        expositions_interval=sim_params.transmission_scale_days + (np.random.random() - 0.5) * 0.1,
    )


def run_simulations(
        sim_params: Parameters,
        simulate_capacity=False,
        duration: int = 80,
        number_of_simulations: int = 4,  # For final presentation purposes, a value greater than 10 is recommended
        simulation_size: int = 100000,  # For final presentation purposes, a value greater than 500000 is recommended
        fpath=None,
        add_noise=True,  # Simulate uncertainty about main parameters and constants
        use_cache=True,
        tqdm=None,  # Optional tqdm function to display progress
):
    if tqdm:
        manager = Manager()
        creation_queue = manager.Queue()
        simulation_queue = manager.Queue()

    simulate_with_params = partial(simulate,
                                   sim_params=sim_params,
                                   simulation_size=simulation_size,
                                   duration=duration,
                                   simulate_capacity=simulate_capacity,
                                   add_noise=add_noise,
                                   use_cache=use_cache,
                                   creation_queue=creation_queue if tqdm else None,
                                   simulation_queue=simulation_queue if tqdm else None,
                                   )
    try:
        pool = Pool(min(cpu_count(), number_of_simulations))
        all_stats = pool.imap(simulate_with_params, range(number_of_simulations))
        if tqdm:
            creation_bar, simulation_bar = show_progress(tqdm, creation_queue, simulation_queue, simulation_size,
                                                         number_of_simulations, duration)
            creation_bar.start()
            simulation_bar.start()
        all_stats = list(all_stats)
    finally:
        pool.close()
        pool.join()
        if tqdm:
            creation_bar.stop()
            creation_bar.join()
            simulation_bar.stop()
            simulation_bar.join()
    stats = combine_stats(all_stats, sim_params)
    if fpath:
        stats.save(fpath)
    return stats


def combine_stats(all_stats: List[np.ndarray], sim_params: Parameters):
    mstats = np.stack(all_stats)
    population_names = tuple(p.name for p in sim_params.population_segments)
    return Stats(mstats, cs.MEASUREMENTS, METRICS, population_names, cs.age_str, start_date=sim_params.start_date)


def show_progress(tqdm, creation_queue: Queue, simulation_queue: Queue, simulation_size: int,
                  number_of_simulations: int, duration: int):
    creation_bar = ProgressBar(tqdm, creation_queue, simulation_size * number_of_simulations, 0, 'Population')
    simulation_bar = ProgressBar(tqdm, simulation_queue, duration * number_of_simulations, 1, 'Simulation')
    return creation_bar, simulation_bar
