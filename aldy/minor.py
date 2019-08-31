# 786

# Aldy source: minor.py
#   This file is subject to the terms and conditions defined in
#   file 'LICENSE', which is part of this source code package.


from typing import List, Dict, Tuple, Set, Callable, Optional
from functools import reduce, partial

import math
import collections
import multiprocessing

from . import lpinterface
from .common import *
from .gene import Mutation, Gene, Allele, Suballele
from .sam import Sample
from .cn import MAX_CN
from .coverage import Coverage
from .major import MajorSolution, SolvedAllele

# Model parameters
MISS_PENALTY_FACTOR = 2.0
"""float: Penalty for each missed minor mutation (0 for no penalty)"""

ADD_PENALTY_FACTOR = 1.0
"""float: Penalty for each novel minor mutation (0 for no penalty)"""


class MinorSolution(collections.namedtuple('MinorSolution', ['score', 'solution', 'major_solution'])):
   """
   Describes a potential (possibly optimal) minor star-allele configuration.
   Immutable class.

   Attributes:
      score (float):
         ILP model error score (0 for user-provided solutions).
      solution (list[:obj:`SolvedAllele`]):
         List of minor star-alleles in the solution.
         Modifications to the minor alleles are represented in :obj:`SolvedAllele` format.
      major_solution (:obj:`aldy.major.MajorSolution`):
         Major star-allele solution used for calculating the minor star-allele assignment.
      diplotype (str):
         Assigned diplotype string (e.g. ``*1/*2``).

   Notes:
      Has custom printer (``__str__``).
   """
   diplotype = ''


   def _solution_nice(self):
      return ', '.join(str(s) 
                       for s in sorted(self.solution, 
                                       key=lambda x: allele_sort_key(x.minor)))

   
   def __str__(self):
      return f'MinorSol[{self.score:.2f}; ' + \
             f'sol=({self._solution_nice()}); ' + \
             f'major={self.major_solution}'


def estimate_minor(gene: Gene, 
                   coverage: Coverage, 
                   major_sols: List[MajorSolution], 
                   solver: str,
                   filter_fn: Optional[Callable] = None) -> List[MinorSolution]:
   """
   Detect the minor star-allele in the sample.

   Args:
      gene (:obj:`aldy.gene.Gene`): 
         A gene instance.
      coverage (:obj:`aldy.coverage.Coverage`): 
         Read alignment data.
      major_sol (:obj:`aldy.major.MajorSolution`): 
         Copy-number solution to be used for major star-allele calling.
      solver (str): 
         ILP solver to use. Check :obj:`aldy.lpinterface` for available solvers.
      filter_fn (callable):
         Custom filtering function (used for testing only).

   Returns:
      list[:obj:`MinorSolution`]
   """

   # Get list of alleles and mutations to consider
   alleles: List[Tuple[str, str]] = list()
   # Consider all major and minor mutations *from all available major solutions* together
   mutations: Set[Mutation] = set()
   for major_sol in major_sols:
      for (ma, _, added, _) in major_sol.solution:
         alleles += [SolvedAllele(ma, mi, added, tuple()) for mi in gene.alleles[ma].minors]
         mutations |= set(gene.alleles[ma].func_muts)
         mutations |= set(added)
         for sa in gene.alleles[ma].minors.values():
            mutations |= set(sa.neutral_muts)

   # Filter out low quality mutations
   def default_filter_fn(mut, cov, total, thres):
      # TODO: is this necessary?
      if mut.op != '_' and mut not in mutations: 
         return False
      return Coverage.basic_filter(mut, cov, total, thres / MAX_CN) and \
             Coverage.cn_filter(mut, cov, total, thres, major_sol.cn_solution) 
   if filter_fn:
      cov = coverage.filtered(filter_fn)
   else:
      cov = coverage.filtered(default_filter_fn)
   
   minor_sols = []
   for major_sol in sorted(major_sols, key=lambda s: list(s.solution.items())):
      minor_sols += solve_minor_model(gene, alleles, cov, major_sol, mutations, solver)
   return minor_sols


def solve_minor_model(gene: Gene,
                      alleles_list: List[SolvedAllele], 
                      coverage: Coverage, 
                      major_sol: MajorSolution, 
                      mutations: Set[Mutation], 
                      solver: str) -> List[MinorSolution]:
   """
   Solves the minor star-allele detection problem via integer linear programming.

   Args:
      gene (:obj:`aldy.gene.Gene`):
         Gene instance.
      alleles_list (list[:obj:`aldy.major.SolvedAllele`]):
         List of candidate minor star-alleles. 
      coverage (:obj:`aldy.coverage.Coverage`):
         Sample coverage used to find out the coverage of each mutation.
      major_sol (:obj:`aldy.major.MajorSolution`):
         Major star-allele solution to be used for detecting minor star-alleles (check :obj:`aldy.major.MajorSolution`).
      mutations (set[:obj:`aldy.gene.Mutation`]):
         List of mutations to be considered during the solution build-up 
         (all other mutations are ignored).
      solver (str): 
         ILP solver to use. Check :obj:`aldy.lpinterface` for available solvers.

   Returns:
      list[:obj:`MinorSolution`]
      
   Notes:
      Please see `Aldy paper <https://www.nature.com/articles/s41467-018-03273-1>`_ (section Methods/Genotype refining) for the model explanation.
      Currently returns only the first optimal solution.
   """

   log.debug('\nRefining {}', major_sol)
   model = lpinterface.model('aldy_refine', solver)
   
   # Establish minor allele binary variables
   alleles = {(a, 0): set(gene.alleles[a.major].func_muts) | \
                      set(gene.alleles[a.major].minors[a.minor].neutral_muts) | \
                      set(a.added) 
              for a in alleles_list}

   log.debug('Possible candidates:')
   for a in sorted(alleles, key=lambda x: allele_sort_key(x[0].minor)):
      (ma, mi, _, _), _ = a
      log.debug('  *{} (cn=*{})', mi, gene.alleles[ma].cn_config)
      for m in sorted(alleles[a], key=lambda m: m.pos):
         m_gene, m_region = gene.region_at(m.pos)
         log.debug('    {:26}  {:.2f} ({:4} / {} * {:4}) {}:{} {}',
            str(m), 
            coverage[m] / coverage.single_copy(m.pos, major_sol.cn_solution),
            coverage[m], 
            major_sol.cn_solution.position_cn(m.pos),
            coverage.single_copy(m.pos, major_sol.cn_solution),
            m_gene, m_region,
            m.aux.get('old', ''))
   
   for a, _ in list(alleles):
      max_cn = major_sol.solution[SolvedAllele(a.major, None, a.added, a.missing)]
      for cnt in range(1, max_cn):
         alleles[a, cnt] = alleles[a, 0]
      
   A = {a: model.addVar(vtype='B', name=a[0].minor) for a in alleles}
   for a, cnt in alleles:
      if cnt == 0:
         continue
      model.addConstr(A[a, cnt] <= A[a, cnt - 1])

   # Make sure that sum of all subaleles is exactly as the count of their major alleles
   for sa, cnt in sorted(major_sol.solution.items()):
      expr = model.quicksum(v for ((ma, _, ad, mi), _), v in A.items() 
                            if SolvedAllele(ma, None, ad, mi) == sa)
      log.trace('LP constraint: {} == {} for {}', cnt, expr, a)
      model.addConstr(expr == cnt)

   # Add a binary variable for each allele/mutation pair where mutation belongs to that allele
   # that will indicate whether such mutation will be assigned to that allele or will be missing
   MPRESENT = {a: {m: model.addVar(vtype='B', name=f'P_{m.pos}_{m.op.replace(".", "")}_{a[0].minor}') 
                   for m in sorted(alleles[a])}
               for a in sorted(alleles)}
   # Add a binary variable for each allele/mutation pair where mutation DOES NOT belongs to that allele
   # that will indicate whether such mutation will be assigned to that allele or not
   MADD = {a: {} for a in alleles}
   for a in sorted(MADD):
      for m in sorted(mutations):
         if gene.has_coverage(a[0].major, m.pos) and m not in alleles[a]:
            MADD[a][m] = model.addVar(vtype='B', name=f'A_{m.pos}_{m.op.replace(".", "")}_{a[0].minor}')
   # Add an error variable for each mutation and populate the error constraints
   error_vars = {m: model.addVar(lb=-model.INF, ub=model.INF, name=f'E_{m.pos}_{m.op.replace(".", "")}') 
                 for m in sorted(mutations)}
   constraints = {m: 0 for m in mutations} 
   for m in sorted(mutations):
      for a in sorted(alleles):
         if m in alleles[a]:
            constraints[m] += MPRESENT[a][m] * A[a]
         elif gene.has_coverage(a[0].major, m.pos):
            # Add this *only* if CN of this region in a given allele is positive 
            # (i.e. do not add mutation to allele if a region of mutation is deleted due to the fusion)
            constraints[m] += MADD[a][m] * A[a]

   # Fill the constraints for non-variations (i.e. where nucleotide matches reference genome)
   for pos in sorted(set(m.pos for m in constraints)):
      ref_m = Mutation(pos, '_') # type: ignore
      error_vars[ref_m] = model.addVar(lb=-model.INF, ub=model.INF, name=f'E_{pos}_REF')
      constraints[ref_m] = 0
      for a in sorted(alleles):
         if not gene.has_coverage(a[0].major, pos):
            continue
         # Does this allele contain any mutation at the position `pos`? 
         # Insertions are not counted as they always contribute to `_`.
         present_muts = [m for m in alleles[a] if m.pos == pos and m[1][:3] != 'INS']
         assert(len(present_muts) < 2)
         if len(present_muts) == 1:
            constraints[ref_m] += (1 - MPRESENT[a][present_muts[0]]) * A[a]
         else:
            N = 1
            for m in MADD[a]:
               if m.pos == pos and m[1][:3] != 'INS':
                  N *= 1 - MADD[a][m]
            constraints[ref_m] += N * A[a]

   # Ensure that each constraint matches the observed coverage
   print('  {')
   print(f'    "cn": {str(dict(major_sol.cn_solution.solution))}, ')
   print( '    "major": ' + str({
         tuple([s.major] + [(m[0], m[1]) for m in s.added]) if len(s.added) > 0 else s.major: v
         for s, v in major_sol.solution.items()}) + ", ")
   print('    "data": {', end='')
   for m, expr in sorted(constraints.items()):
      cov = coverage[m] / coverage.single_copy(m.pos, major_sol.cn_solution) 
      model.addConstr(expr + error_vars[m] == cov)
      print(f"({m[0]}, '{m[1]}'): {cov}, ", end='')
   print('}, ')

   # Ensure that a mutation is not assigned to allele that does not exist 
   for a, mv in sorted(MPRESENT.items()):
      for m, v in sorted(mv.items()):
         log.trace('LP contraint: {} >= {} for MPRESENT[{}, {}]', A[a], v, a, m)
         model.addConstr(v <= A[a])
   for a, mv in sorted(MADD.items()):
      for m, v in sorted(mv.items()):
         log.trace('LP contraint: {} <= {} for MADD[{}, {}]', A[a], v, a, m)
         model.addConstr(v <= A[a])

   # Ensure the following rules for all mutations:
   # 1) A minor allele must express ALL its functional mutations
   for a in sorted(alleles):
      p = [MPRESENT[a][m] for m in sorted(alleles[a]) if m.is_functional]
      if len(p) == 0: 
         continue
      expr = model.quicksum(p)
      log.trace('LP constraint 1: {} = {} * A_{} for {}', expr, len(p), A[a], a)
      model.addConstr(expr == len(p) * A[a]) # Either all or none
   # 2) No allele can include mutation with coverage 0
   for a in sorted(MPRESENT):
      for m, v in sorted(MPRESENT[a].items()):
         if not gene.has_coverage(a[0].major, m.pos):
            model.addConstr(v <= 0)
   # 3) No allele can include extra functional mutation (this is resolved at step 2)
   for m in sorted(mutations): 
      if m.is_functional:
         expr = model.quicksum(MADD[a][m] for a in sorted(alleles) if m not in alleles[a])
         log.trace('LP constraint 3: 0 >= {} for {}', expr, m)
         model.addConstr(expr <= 0)
   
   # 4) TODO: CNs at each loci must be respected
   for m in sorted(mutations):
      exprs = (A[a] for a in sorted(alleles) if gene.has_coverage(a[0].major, m.pos))
      expr = model.quicksum(exprs)
      total_cn = major_sol.cn_solution.position_cn(m.pos)
      log.trace(f'LP constraint 4: {total_cn} == {expr} for {m}')
      # print(f'{m}:{m_gene}/{m_region} --> {total_cn} @ {list(exprs)}')
      # floor/ceil because of total_cn potentially having 0.5 as a summand
      # model.addConstr(expr >= math.floor(total_cn))
      # model.addConstr(expr <= math.ceil(total_cn))
   # 5) TODO: Make sure that CN of each variation does not exceed total supporting allele CN
   for m in mutations: 
      m_cn = major_sol.cn_solution.position_cn(m.pos)
      exp_cn = coverage.percentage(m) / 100.0

      # Get lower/upper CN bound: [floor(expressed_cn), ceil(expressed_cn)]
      if m_cn == 0:
         lo, hi = 0, 0
      elif coverage[m] > 0 and int(exp_cn * m_cn) == 0:
         lo, hi = 1, 1 # Force minimal CN to be 1
      else:
         lo, hi = int(exp_cn * m_cn), min(int(exp_cn * m_cn) + 1, m_cn)
            
      expr  = model.quicksum(MPRESENT[a][m] * A[a] for a in alleles if m in alleles[a])
      expr += model.quicksum(MADD[a][m] * A[a] for a in alleles if m not in alleles[a])

      assert(lo >= 0)
      assert(hi <= m_cn)
      log.trace('LP constraint 5: {} <= {} <= {} for {}', m, lo, expr, hi, m)
      #model.addConstr(expr >= lo); model.addConstr(expr <= hi)

   # Objective: absolute sum of errors
   objective = model.abssum(v for _, v in sorted(error_vars.items()))
   if solver == 'scip':
      # HACK: Non-linear objective linearization for SCIP:
      #       min f(x) <==> min w s.t. f(x) <= w
      w = model.addVar(name='W')
      nonlinear_obj = 0
      for a in alleles:
         nonlinear_obj += MISS_PENALTY_FACTOR * A[a] * \
                          model.quicksum((1 - v) for m, v in MPRESENT[a].items())
         objective += ADD_PENALTY_FACTOR * \
                      model.quicksum(v for m, v in MADD[a].items())
      model.addConstr(nonlinear_obj <= w)
      objective += w
   else:
      objective += MISS_PENALTY_FACTOR * \
                   model.quicksum(A[a] * (1 - v) for a in sorted(alleles) for _, v in sorted(MPRESENT[a].items()))
      objective += ADD_PENALTY_FACTOR * \
                   model.quicksum(v for a in sorted(alleles) for _, v in sorted(MADD[a].items()))
   log.trace('Objective: {}', objective)

   # Solve the model
   try:
      status, opt = model.solve(objective)
      model.model.write('minor.lp')
      solution = []
      for allele, value in A.items():
         if model.getValue(value) <= 0:
            continue
         added: List[Mutation] = []
         missing: List[Mutation] = []
         for m, mv in MPRESENT[allele].items():
            if not model.getValue(mv):
               missing.append(m)
         for m, mv in MADD[allele].items():
            if model.getValue(mv):
               added.append(m)
         solution.append(SolvedAllele(allele[0].major,
                                      allele[0].minor,
                                      allele[0].added + tuple(added),
                                      tuple(missing)))
      print('    "sol": ' + str([
          (s.minor, [(m[0], m[1]) for m in s.added], [(m[0], m[1]) for m in s.missing])
          for s in solution]))
      sol = MinorSolution(score=opt,
                           solution=solution,
                           major_solution=major_sol)
      log.debug(f'Minor solution: {sol}')
      return [sol]
   except lpinterface.NoSolutionsError:
      return []

