import math, statistics, datetime as dt
import json, warnings, os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from models import returnGenerator, annuity, model, socialSecurity
import git, sys
git_root= git.Repo(os.path.abspath(''),
                   search_parent_directories=True).git.rev_parse('--show-toplevel')
sys.path.append(git_root)
from models import returnGenerator, annuity, model
from data import constants as const

# For reference, something that has a 3% growth is a 0.03 return/rate and 1.03 yield. That's how I'll define return/rate and yield here

DEBUG_LVL = 1 # LVL 1: Print success rate, save worst failure, show plot | LVL 2: Investigate each result 1 by 1
SAVE_DIR = os.path.join(git_root,'diagnostics/saved')
TODAY = dt.date.today()
TODAY_QUARTER = (TODAY.month-1)//3
TODAY_YR = TODAY.year
TODAY_YR_QT = TODAY_YR+TODAY_QUARTER*.25
MONTE_CARLO_RUNS = 500 # takes 20 seconds to generate 5000
if os.path.exists(SAVE_DIR):
    for file in os.scandir(SAVE_DIR): # delete previously saved files
        os.remove(file.path)
else:
    os.makedirs(SAVE_DIR)

class Simulator:
    def __init__(self,param_vals,override_dict={}):
        self.params = self._clean_data(param_vals)
        self.rows = int((param_vals['Calculate Til'] - TODAY_YR_QT)/.25)
        self.fi_date = self.params["FI Quarter"]
        self.admin = self.params["Admin"] # Are you Chris?
        self.override_dict = override_dict
            
    def main(self):
# -------------------------------- VARIABLES -------------------------------- #      
    # STATIC LISTS: TIME, JOB INCOME --------------------------------------- #
        debug_lvl = DEBUG_LVL
        FLAT_INFLATION = self._val("Flat Inflation (%)",QT_MOD=False) # Used for some estimations like pension
        working_qts = int((self.fi_date-TODAY_YR_QT)/.25)
        options= {
            'debug_lvl': DEBUG_LVL,
            'flat_inflation': FLAT_INFLATION,
            'flat_inflation_qt': FLAT_INFLATION ** (1. / 4),
            'time_ls': self._range_len(START=TODAY_YR_QT,LEN=self.rows,INCREMENT=0.25,ADD=True),
            'working_qts': working_qts,
            'FI_qts': self.rows-working_qts,
            'barista_qts': 4 * self._val("Barista Time (Yrs)",QT_MOD=False),
            'equity_target': self._val("Equity Target",False)
            }
        
        # Year.Quarter list 
        time_ls = self._range_len(START=TODAY_YR_QT,LEN=self.rows,INCREMENT=0.25,ADD=True) 
        FI_qts = self.rows-working_qts
        barista_qts = 4 * self._val("Barista Time (Yrs)",QT_MOD=False)
        
        # Job Income and tax-differed list. Does not include SS. 
            # get quarterly income for his and her
        his_qt_income = self._val("His Total Income",QT_MOD='dollar')
        her_qt_income = self._val("Her Total Income",QT_MOD='dollar')
        total_income_qt = his_qt_income+her_qt_income
        tax_deferred_qt = self._val("His Tax Deferred",QT_MOD='dollar')+self._val("Her Tax Deferred",QT_MOD='dollar')
        total_barista_income_qt = self._val("Barista Income (Total)", QT_MOD='dollar') # Assuming no tax deferral for barista to be conservative and keep it easier
            # build out income lists with raises coming in steps on the first quarter of each year
        raise_yr = 1+self._val("Raise (%)",QT_MOD=False)
        usr_income_ls = self._step_quarterize(his_qt_income,raise_yr,mode='working',working_qts=working_qts) if working_qts !=0 else []
        partner_income_ls = self._step_quarterize(her_qt_income,raise_yr,mode='working',working_qts=working_qts) if working_qts !=0 else []
        job_income_ls = list(np.array(usr_income_ls)+np.array(partner_income_ls))
        tax_deferred_ls = self._step_quarterize(tax_deferred_qt,raise_yr,mode='working',working_qts=working_qts) if working_qts !=0 else []
        barista_income_ls = self._range_len(START=total_barista_income_qt,LEN=barista_qts,INCREMENT=FLAT_INFLATION,MULT=True) if total_barista_income_qt != 0 else [] # smooth growth is probably fine rather than step_quarterizing
            # add the non-working years
        #TODO: #57 Remove job_income_ls and replace with list of incomes (usr_income_1, partner_income_1, usr_income_2, etc)
        job_income_ls  = job_income_ls + barista_income_ls + ([0] * (FI_qts - barista_qts)) 
        tax_deferred_ls = tax_deferred_ls + ([0]*FI_qts) 

        
    # MONTE CARLO VARIED LISTS: RETURN, INFLATION, SPENDING, ALLOCATION, NET WORTH ------------ #
        # variables that don't alter with each run
        if 'monte_carlo_runs' in self.override_dict:
            monte_carlo_runs = self.override_dict['monte_carlo_runs']
        else:
            monte_carlo_runs = MONTE_CARLO_RUNS
        if 'returns' in self.override_dict:
            stock_return_arr,bond_return_arr,re_return_arr,inflation_arr = self.override_dict['returns']
        else:
            stock_return_arr,bond_return_arr,re_return_arr,inflation_arr = returnGenerator.main(self.rows,4,monte_carlo_runs) # bring in generated returns. Would prefer to use multiprocessing, but can't figure out how to get arrays of arrays handed back in .Value()
        spending_qt = self._val("Total Spending (Yearly)",QT_MOD='dollar')
        retirement_change = self._val("Retirement Change (%)",QT_MOD=False) # reduction of spending expected at retirement (less driving, less expensive cost of living, etc)
            # make a kids array with years of kids being planned
        kid_year_qts = list(str(self._val("Kid Birth Years",QT_MOD=False)).split(",")) # have to force it to be a string if only one kid
        if kid_year_qts != ['']:
            kid_year_qts = [float(year_qt) for year_qt in kid_year_qts] 
        else: kid_year_qts =[] # needs to be an empty array for kid_ls to compute
        kid_spending_rate = self._val("Cost of Kid (% Spending)",QT_MOD=False)
        # performance tracking
        success_rate = 0
        final_net_worths = [] # Establish empty list to calculate net worth median. Chris: Preference on using "_ls" here or reserving "_ls" only for the lists representing each period?
        worst_failure_idx = self.rows
        failure_dict ={}
        
        # Monte Carlo
        for col in range(monte_carlo_runs):
            stock_return_ls = stock_return_arr[col]
            bond_return_ls = bond_return_arr[col]
            re_return_ls = re_return_arr[col]
            inflation_ls = inflation_arr[col]
            
            # Social Security Initialization
            usr_ss_calc = socialSecurity.Calculator(self,'User',inflation_ls,time_ls,usr_income_ls)
            partner_ss_calc = socialSecurity.Calculator(self,'Partner',inflation_ls,time_ls,partner_income_ls)
           
                # FICA: Medicare (1.45% of income) and social security (6.2% of eligible income). Her income excluded from SS due to pension
            #medicare = [0.0145*job_income for job_income in job_income_ls]
            medicare= np.array(job_income_ls)*0.0145
                # need the SS Max Earnings, but in quarter form instead of the annual form I did in the SS section.
            ss_max_earnings_qt = self._step_quarterize(0.25 * socialSecurity.est_Max_Earning(TODAY_YR),FLAT_INFLATION,mode='working',working_qts=working_qts + barista_qts)
            his_income_ratio = his_qt_income/(his_qt_income+her_qt_income)
            ss_tax = [0.062*min(his_income_ratio*income,ss_max) for income,ss_max in zip(job_income_ls,ss_max_earnings_qt)]
            ss_tax+= [0]*(self.rows-len(ss_tax))
            
            # Kid count   
                # kids_ls should have kid for every year from each kid's birth till 22 years after
            kids_ls = [0]*self.rows
            for kid_yr in kid_year_qts:
                kids_ls = [other_kids + 1 if yr_qt>=kid_yr and yr_qt-22<kid_yr else other_kids 
                        for other_kids,yr_qt in zip(kids_ls,time_ls) ]
            
            # Net Worth/total savings
            spending_ls, total_costs_ls, net_transaction_ls, equity_alloc_ls = [],[],[],[]
            re_alloc_ls, bond_alloc_ls, taxes_ls, total_income_ls, usr_ss_ls, partner_ss_ls = [],[],[],[],[],[]
            return_rate = None
            my_annuity = annuity.Annuity(interest_yield_qt=const.ANNUITY_INT_YIELD ** (1/4),
                                         payout_rate_qt=const.ANNUITY_PAYOUT_RATE/4,time_ls=time_ls)
                # loop through time_ls to find net worth changes
            net_worth_ls = [self._val('Current Net Worth ($)',QT_MOD=False)]
            for row in range(self.rows): 
                # allocations
                alloc = self.allocation(inflation=inflation_ls[row],
                                        net_worth = net_worth_ls[-1])
                equity_alloc_ls.append(alloc["Equity"])
                re_alloc_ls.append(alloc["RE"])
                bond_alloc_ls.append(alloc["Bond"])
                # social security 
                usr_ss_ls.append(usr_ss_calc.get_payment(row,net_worth_ls[-1],self._val("Equity Target",QT_MOD=False)))
                partner_ss_ls.append(partner_ss_calc.get_payment(row,net_worth_ls[-1],self._val("Equity Target",QT_MOD=False)))
                if self.admin: 
                    # add denica pension if you're Chris
                    partner_ss_ls[-1] += self.get_pension_payment(her_qt_income, raise_yr, row, inflation_ls, net_worth_ls[-1], options) 
                # taxes
                # taxes are 80% for pension and social security. Could optimze by skipping when sum of income is 0
                income_tax = get_taxes(job_income_ls[row]-tax_deferred_ls[row])+0.8*get_taxes(usr_ss_ls[row]+ partner_ss_ls[row])
                taxes_ls.append(income_tax + medicare[row] + ss_tax[row])
                # spending
                working = True if row<working_qts else False
                spending_ls.append(self.base_spending(spending_qt, retirement_change,
                                                      inflation=inflation_ls[row], 
                                                 working=working, alloc=alloc,return_rate=return_rate))
                kids_ls[row] = spending_ls[row] * kid_spending_rate * kids_ls[row]
                total_costs_ls.append(taxes_ls[row] + spending_ls[row] + kids_ls[row])
                total_income_ls.append(job_income_ls[row]+usr_ss_ls[row]+ partner_ss_ls[row])
                net_transaction_ls.append(total_income_ls[row] - total_costs_ls[row])
                # annuity contributions
                if alloc['Annuity'] != 0: 
                    amount = alloc['Annuity'] * net_worth_ls[-1]
                    my_annuity.contribute(amount=amount,date=time_ls[row])
                    net_worth_ls[-1] -= amount
                # investment returns
                return_rate = stock_return_ls[row]*alloc['Equity'] + bond_return_ls[row]*alloc['Bond'] + re_return_ls[row]*alloc['RE']
                return_amt = return_rate*(net_worth_ls[-1]+0.5*net_transaction_ls[row])
                # annuity withdrawals
                if net_worth_ls[-1]+return_amt+net_transaction_ls[row] < 0 and not my_annuity.annuitized:
                    my_annuity.annuitize(time_ls[row])
                if my_annuity.annuitized:
                    net_transaction_ls[row] += my_annuity.take_payment(time_ls[row])
                net_worth_ls.append(max(0,net_worth_ls[-1]+return_amt+net_transaction_ls[row]))
            net_worth_ls.pop()
            if net_worth_ls[-1]!=0: 
                success_rate += 1
            final_net_worths.append(net_worth_ls[-1]) # Add final net worth to list for later calculation
            if 0 in net_worth_ls and net_worth_ls.index(0) < worst_failure_idx and debug_lvl >= 1:
                worst_failure_idx = net_worth_ls.index(0)
                failure_dict = {
                    "Time":time_ls,
                    "Net Worth":net_worth_ls,
                    "Job Income":job_income_ls,
                    "Tax Deferred":tax_deferred_ls,
                    "User SS":usr_ss_ls,
                    "Partner SS":partner_ss_ls,
                    "Total Income":total_income_ls,
                    "Total Taxes":taxes_ls,
                    "Inflation":inflation_ls,
                    "Spending":spending_ls,
                    "Kid Costs":kids_ls,
                    "Total Costs":total_costs_ls,
                    "Net Transaction":net_transaction_ls,
                    "Stock Alloc":equity_alloc_ls,
                    "Bond Alloc":bond_alloc_ls,
                    "RE Alloc":re_alloc_ls,
                    "Stock Returns":stock_return_ls,
                    "Bond Returns":bond_return_ls,
                    "RE Returns":re_return_ls
                }
            if debug_lvl >= 1: plt.plot(time_ls,net_worth_ls)
            if debug_lvl >= 2: 
                plt.show()
                usr_input = input("save (s), next (n), continue (c)?")
                if usr_input == 's':
                    save_dict = {
                        "Time":time_ls,
                        "Net Worth":net_worth_ls,
                        "Job Income":job_income_ls,
                        "Tax Deferred":tax_deferred_ls,
                        "User SS":usr_ss_ls,
                        "Partner SS":partner_ss_ls,
                        "Total Income":total_income_ls,
                        "Total Taxes":taxes_ls,
                        "Inflation":inflation_ls,
                        "Spending":spending_ls,
                        "Kid Costs":kids_ls,
                        "Total Costs":total_costs_ls,
                        "Net Transaction":net_transaction_ls,
                        "Stock Alloc":equity_alloc_ls,
                        "Bond Alloc":bond_alloc_ls,
                        "RE Alloc":re_alloc_ls,
                        "Stock Returns":stock_return_ls,
                        "Bond Returns":bond_return_ls,
                        "RE Returns":re_return_ls
                    }
                    save_df = pd.DataFrame.from_dict(save_dict)
                    save_df.to_csv(f'{SAVE_DIR}/saveData{col}.csv')
                elif usr_input == 'c':
                    debug_lvl = 1
        success_rate = success_rate/monte_carlo_runs
        median_net_worth = statistics.median(final_net_worths)
        if debug_lvl >= 1: 
            failure_df = pd.DataFrame.from_dict(failure_dict)
            failure_df.to_csv(f'{SAVE_DIR}/worst_failure.csv')
            print(f"Success Rate: {success_rate*100:.2f}%")
            print(f"Median Final Net Worth: ${median_net_worth*1000:,.0f}")
        
        if debug_lvl >= 1: plt.show()
        return success_rate, [stock_return_arr,bond_return_arr,re_return_arr,inflation_arr]
        
        debug_point = None
        

    # HELPER FUNCTIONS ---------------------------------------------------- #
    
    def _step_quarterize(self,first_val,increase_yield,mode,**kw) -> list:
        """Return a list with values that step up on a yearly basis rather than quarterly \n
        mode = 'working' -> from today_qt to fi_date, needs kw['working_qts'] \n
        mode = 'pension' -> from provided kw['start_yr'] to end of kw['time_ls']"""
        ls = [first_val]
        if mode == 'working':
            if kw["working_qts"] == 0: return [] # if fi_date = TODAY_YR_QT, the returned range should be empty
            custom_range = self._range_len(START=TODAY_QUARTER+1,LEN=kw["working_qts"]-1,INCREMENT=1,ADD=True) # subtracing one len since you already have the first value
            [ls.append(ls[-1]) if x%4 !=0 else ls.append(ls[-1]*increase_yield) for x in custom_range]
        elif mode == 'pension':
            custom_range = np.arange(kw['start_yr']+0.25,kw['time_ls'][-1]+0.25,0.25)
            [ls.append(ls[-1]*increase_yield) if x%1 ==0 else ls.append(ls[-1]) for x in custom_range]
        return ls
                
    def _catch(self, func, *args, handle=lambda e : e, **kwargs):
        # https://stackoverflow.com/questions/1528237/how-to-handle-exceptions-in-a-list-comprehensions
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # not sure how to implement this, so just hardcoding in return 0 for now, probably need to learn lambdas better
            # return handle(e) 
            return 0
    
    def _pow(self,num,exp:int) -> int:
        """exponential formular num^exp that should be faster for small exponents"""
        i=1
        result = num
        while i<exp:
            result = result * num
            i+=1
        return result
    
    def _val(self,KEY:str,QT_MOD):
        """MOD='rate' will return (1+r)^(1/4) \n 
        MOD='dollar' will return d/4 \n 
        MOD=False will return value"""
        if QT_MOD=="rate":
            return (1+self.params[KEY]) ** (1. / 4)
        elif QT_MOD=='dollar':
            return self.params[KEY] / 4
        elif not QT_MOD:
            return self.params[KEY]
        else:
            raise Exception("invalid MOD")
            
    
    def _range_len(self,START,LEN:int,INCREMENT,MULT=False,ADD=False) -> list:
        """Provide a range with a set START and set LENgth. If MULT set to True, Increment should be in yield (1+rate, ei: 1.03) format."""
        if ADD:
            return list(np.linspace(start=START,stop=START+INCREMENT*LEN,num=LEN,endpoint=False))
        elif MULT:
            # https://chrissardegna.com/blog/python-expontentiation-performance/
            return list(np.geomspace(start=START,stop=START*INCREMENT**LEN,num=LEN,endpoint=False))
        else:
            raise Exception("Didn't declare either MULT or ADD")
    
    def _clean_data(self, params: dict) -> dict:
            for k, v in params.items():
                if type(v) is dict:
                    continue
                elif v.isdigit():
                    params[k] = int(v)
                elif self._is_float(v):
                    params[k] = float(v)
                elif v == "True":
                    params[k] = True
                elif v == "False":
                    params[k] = False
            return params
        
    def _is_float(self, element: any) -> bool:
        try:
            float(element)
            return True
        except ValueError:
            return False
        
    def get_pension_payment(self, partner_qt_income, raise_yr, row, inflation_ls, net_worth, options):
        """
        Calculates the pension for Chris's wife. Might want to generalize this

        Parameters
        ----------
        partner_qt_income : int or float
            DESCRIPTION.
        raise_yr : int or float
            DESCRIPTION.
        options : dict
            The required entries for the dict are flat_inflation, time_ls, 
            working_qts, FI_qts

        Returns
        -------
        pension_ls : list
            Values for each quarter.

        """
        if hasattr(self, 'pension_ls'):
            if row == 0: del self.pension_ls # reset for each loop
            else: return self.pension_ls[row]
        
        EARLY_YEAR = 2043
        MID_YEAR = 2049
        LATE_YEAR = 2055
        FLAT_INFLATION= options['flat_inflation']
        time_ls= options['time_ls']
        working_qts= options['working_qts']
        FI_qts= options['FI_qts']
        equity_target = options['equity_target']
        method = self._val('Pension Method',QT_MOD=False)
            # Calc max salary estimate
        fi_yr = math.trunc(self.fi_date)
        current_pension_salary_qt = partner_qt_income/0.91 # Corrects for 9% taken from salary for pension
        if method == 'cash-out':
            # Need to correct for out-dated info, first estimate salary at time of last update, then project forward
            data_age_qt = int((TODAY_YR_QT - const.PENSION_ACCOUNT_BAL_UP_DATE)/.25) # find age of data
            est_prev_pension_salary_qt = current_pension_salary_qt / (raise_yr ** (data_age_qt/4)) # estimate salary at time of data
            projected_income = self._step_quarterize(est_prev_pension_salary_qt,raise_yr,mode='working',working_qts=working_qts + data_age_qt) # project income from data age to FI
            pension_bal = const.PENSION_ACCOUNT_BAL
            pension_int_rate = const.PENSION_INTEREST_YIELD ** (1/4) - 1
            for income in projected_income:
                pension_bal += income * const.PENSION_COST + pension_bal * pension_int_rate
            self.pension_ls = [0] * working_qts + [pension_bal] + [0] * (FI_qts-1)
            return self.pension_ls[row]
        elif method == 'net worth':
            if (net_worth > equity_target * inflation_ls[row] and time_ls[row]<LATE_YEAR) or time_ls[row]<EARLY_YEAR:
                return 0 # haven't triggered yet
            else:
                pension_start_yr = min(math.trunc(time_ls[row]),LATE_YEAR)
        elif method == 'early':
            pension_start_yr = EARLY_YEAR
        elif method == 'mid':
            pension_start_yr = MID_YEAR
        elif method == 'late':
            pension_start_yr = LATE_YEAR
        remaining_working_years = fi_yr-TODAY_YR-1
        max_pension_salary_qt = current_pension_salary_qt * raise_yr ** remaining_working_years
            # find initial pension amount (in last working year's dollars)
        DE_ANZA_START_YEAR = 2016
        years_worked = fi_yr-DE_ANZA_START_YEAR
        pension_multiplier = const.DENICA_PENSION_RATES[str(pension_start_yr)]
        starting_pension_qt = max_pension_salary_qt * years_worked * pension_multiplier 
            # convert to est. value at pension_start_yr
        starting_pension_qt = starting_pension_qt*self._pow(FLAT_INFLATION,exp=(pension_start_yr-fi_yr))
            # build out list, add the correct number of zeros to the beginning
        self.pension_ls =self._step_quarterize(starting_pension_qt,raise_yr,mode='pension',start_yr=pension_start_yr,time_ls=time_ls)
        self.pension_ls = [0]*(self.rows-len(self.pension_ls))+self.pension_ls
        
        return self.pension_ls[row]
    
    def base_spending(self,spending_qt, retirement_change,**kw):
        """
        Calculates base spending in a quarter

        Parameters
        ----------
        spending_qt : numeric
            DESCRIPTION.
        retirement_change : numeric
            DESCRIPTION.

        Returns
        -------
        spending : numeric
            Dollar value of spending for one quarter.

        """
        # Spending, kids, costs, contributions
        method= self._val("Spending Method",QT_MOD=False)
        inflation = kw['inflation']
        if method == 'inflation-only':
            spending = spending_qt*inflation if kw['working'] else spending_qt*inflation*(1+retirement_change)
        elif method == 'ceil-floor':
            max_flux = self._val("Allowed Fluctuation (%)",QT_MOD=False)
            # real spending should not increase/decrease more than the max_flux (should it be symetric?)
            # only takes effect after retirement
            # reactant to market, not sure how. Maybe try to maintain last withdrawal percentage til you reach max_flux?
                # could use a pre-set withdrawal rate?
                # could just swing back and forth depending if markets are above/below average
            equity_mean_qt = const.EQUITY_MEAN ** (1/4) - 1
            bond_mean_qt =  const.BOND_MEAN ** (1/4) - 1
            re_mean_qt = const.RE_MEAN ** (1/4) - 1
            expected_return_rate_qt = equity_mean_qt *kw['alloc']['Equity'] + bond_mean_qt *kw['alloc']['Bond'] + re_mean_qt *kw['alloc']['RE']
            spending = spending_qt*inflation if kw['working'] else spending_qt*inflation*(1+retirement_change)
            if kw['return_rate'] is not None:
                spending = spending*(1+max_flux) if kw['return_rate'] > expected_return_rate_qt else spending*(1-max_flux)
        return spending
    
    def allocation(self, inflation, **kw):
        """
        Calculates allocation between equity, RE and bonds. 
        Allows for different methods to be designed
        
        Parameters
        ----------
        inflation : numeric
            quarterly rate of inflation
        
        Returns 
        -------
        output: dict 
            {"Equity":EquityAlloc, "RE":REAlloc, "Bond":BondAlloc} 
        """
        method= self._val("Allocation Method",QT_MOD=False)
        re_ratio = self.params["RE Ratio"]
        equity_target = self.params["Equity Target"] 
        max_risk_factor = 1 # You could put this in params if you wanted to be able to modify max risk (in the case of using margin) 
        
        if method == 'Life Cycle':
            #method = 'Life Cycle' -> kw['net_worth']
            equity_target_PV = equity_target*inflation # going to differ to from the Google Sheet since equity target was pegged to FI years rather than today's dollars
            risk_factor = min(max(equity_target_PV/max(kw['net_worth'],0.000001),0),max_risk_factor) # need to avoid ZeroDivisionError
            with warnings.catch_warnings(): # https://stackoverflow.com/a/14463362/13627745 # another way to avoid ZeroDivisionError, but also avoid printing out exceptions
                warnings.simplefilter("ignore")
                try: 
                    re_alloc = (risk_factor*re_ratio)/((1-re_ratio)*(1+risk_factor*re_ratio/(1-re_ratio))) # derived with fun algebra! ReAlloc = RERatio*(ReAlloc+EquityTotal); EquityTotal = RiskFactor*OriginalEquity; ReAlloc+OriginalEquity=100%
                except ZeroDivisionError: 
                    re_alloc = (risk_factor*re_ratio)
            equity_alloc = (1-re_alloc)*risk_factor
            bond_alloc = max(1-re_alloc-equity_alloc,0) 
            output= {"Equity":equity_alloc,
                     "RE":re_alloc,
                     "Bond":bond_alloc,
                     "Annuity":0}
        elif method == 'Life Cycle Annuity':
            equity_target_PV = equity_target*inflation # going to differ to from the Google Sheet since equity target was pegged to FI years rather than today's dollars
            risk_factor = min(max(equity_target_PV/max(kw['net_worth'],0.000001),0),max_risk_factor) # need to avoid ZeroDivisionError
            with warnings.catch_warnings(): # https://stackoverflow.com/a/14463362/13627745 # another way to avoid ZeroDivisionError, but also avoid printing out exceptions
                warnings.simplefilter("ignore")
                try: 
                    re_alloc = (risk_factor*re_ratio)/((1-re_ratio)*(1+risk_factor*re_ratio/(1-re_ratio))) # derived with fun algebra! ReAlloc = RERatio*(ReAlloc+EquityTotal); EquityTotal = RiskFactor*OriginalEquity; ReAlloc+OriginalEquity=100%
                except ZeroDivisionError: 
                    re_alloc = (risk_factor*re_ratio)
            equity_alloc = (1-re_alloc)*risk_factor
            annuity_alloc = max(1-re_alloc-equity_alloc,0) 
            output= {"Equity":equity_alloc,
                     "RE":re_alloc,
                     "Bond":0,
                     "Annuity":annuity_alloc}
        else: 
            raise ValueError("Allocation method is not defined")
        return output
    
# ADDITIONAL HELPER FUNCTIONS ------------------------------------------- #
#These functions do not requre the class
def get_taxes(income_qt):
    """
    Combines federal and state taxes on non-tax-deferred income

    Parameters
    ----------
    income_qt : numeric
        income for a given quarter.

    Returns
    -------
    float
        taxes for a given quarter.

    """
    # Taxes (brackets are for yearly, not qt, so need conversion)
    fed_taxes = bracket_math(const.FED_BRACKET_RATES,max(4*income_qt-const.FED_STD_DEDUCTION,0))
    state_taxes = bracket_math(const.CA_BRACKET_RATES,max(4*income_qt-const.CA_STD_DEDUCTION,0))
    return 0.25 * (fed_taxes+state_taxes) # need to return quarterly taxes

def bracket_math(bracket:list,income):
    rates,bend_points = zip(*bracket) # reverses the more readable format in the json file to the easier to use format for comprehension
    rates,bend_points = list(rates), list(bend_points) # they unzip as tuples for some reason
    bend_points += [income]
    bend_points.sort()
    bend_points = bend_points[:bend_points.index(income)+1]
    return sum([(bend_points[i]-bend_points[i-1])*rate if i!=0 else bend*rate for (i,bend), rate 
        in zip(enumerate(bend_points),rates)])

# JUST FOR TESTING ----------------------------------------------------- #

params = model.load_params()
param_vals = {key:obj["val"] for (key,obj) in params.items()}


if __name__ == '__main__':
    #instantiate a Simulator with the loaded parameters
    test_simulator = Simulator(param_vals)
    test_simulator.main()