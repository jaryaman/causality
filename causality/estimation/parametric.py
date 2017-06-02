import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.robust.robust_linear_model import RLM
from statsmodels.discrete.discrete_model import Logit
from sklearn.neighbors import NearestNeighbors
from causality.util import bootstrap_statistic
import numpy as np


class DifferenceInDifferences(object):
    def __init__(self, robust=True):
        """
        We will take a dataframe where each row is a user,
        and the columns are:
        (1) Assignment: 1 = test, 0 = control
        (2) Start: The value of the metric you're interested
            in at the start of the experiment.
        (3) End: The value of the metric you're interested in
            at the end of the experiment.
        """
        if robust:
            self.model = RLM
        else:
            self.model = OLS

    def average_treatment_effect(self, X, start='Start', end='End', assignment='Assignment'):
        test = X[X['Assignment']==1][['Start','End']]
        control = X[X['Assignment']==0][['Start','End']]
        del X

        test_initial = test['Start']
        test_final = test['End']
        control_initial = control['Start']
        control_final = control['End']
        del test, control

        df = pd.DataFrame({'y' : test_initial, 
                   'assignment' : [1. for i in test_initial], 
                   't' :[0. for i in test_initial] })
        df = df.append(pd.DataFrame({'y' : test_final, 
                                     'assignment' : [1. for i in test_final], 
                                     't' :[1. for i in test_final] }))

        df = df.append(pd.DataFrame({'y' : control_initial, 
                                     'assignment' : [0. for i in control_initial], 
                                     't' :[0. for i in control_initial] }))

        df = df.append(pd.DataFrame({'y' : control_final, 
                                     'assignment' : [0. for i in control_final], 
                                     't' :[1. for i in control_final] }))
        del test_initial, test_final, control_initial, control_final
        df['did'] = df['t'] * df['assignment'] 
        df['intercept'] = 1.

        model = self.model(df['y'], df[['t', 'assignment','did', 'intercept']])
        result = model.fit()
        conf_int = result.conf_int().ix['did']
        expected = result.params['did']
        return conf_int[0], expected, conf_int[1]
        
    def test_parallel_trend(self, X, start='Start', end='End', assignment='Assignment'):
        """
        This will find the average treatment effect on
        a dataset before the experiment is run, to make
        sure that it is zero.  This tests the assumption
        that the average treatment effect between the test
        and control groups when neither is treated is 0.

        The format for this dataset is the same as that 
        for the real estimation task, except that the start
        time is some time before the experiment is run, and
        the end time is the starting point for the experiment.
        """
        lower, exp, upper = self.average_treatment_effect(X,start=start, end=end, assignment=assignment)
        if lower <= 0 <= upper:
            return True
        return False


class PropensityScoreMatching(object):
    def __init__(self):
        # change the model if there are multiple matches per treated!
        self.propensity_score_model = None


    def score(self, X, confounder_types, assignment='assignment', store_model_fit=False, intercept=True):
        """
        Fit a propensity score model using the data in X and the confounders listed in confounder_types. This adds
        the propensity scores to the dataframe, and returns the new dataframe.

        :param X: The data set, with (at least) an assignment, set of confounders, and an outcome
        :param assignment: A categorical variable (currently, 0 or 1) indicating test or control group, resp.
        :param outcome: The outcome of interest.  Should be real-valued or ordinal.
        :param confounder_types: A dictionary of variable_name: variable_type pairs of strings, where
        variable_type is in {'c', 'o', 'd'}, for 'continuous', 'ordinal', and 'discrete'.
        :param store_model_fit: boolean, Whether to store the model as an attribute of the class, as
        self.propensity_score_model
        :param intercept: Whether to include an intercept in the logistic regression model
        :return: A new dataframe with the propensity scores included
        """
        df = X[[assignment]]
        regression_confounders = []
        for confounder, var_type in confounder_types.items():
            if var_type == 'o' or var_type == 'u':
                c_dummies = pd.get_dummies(X[[confounder]], prefix=confounder)
                if len(c_dummies.columns) == 1:
                    df[c_dummies.columns] = c_dummies[c_dummies.columns]
                    regression_confounders.extend(c_dummies.columns)
                else:
                    df[c_dummies.columns[1:]] = c_dummies[c_dummies.columns[1:]]
                    regression_confounders.extend(c_dummies.columns[1:])
            else:
                regression_confounders.append(confounder)
                df.loc[:,confounder] = X[confounder].copy() #
                df.loc[:,confounder] = X[confounder].copy() #
        if intercept:
            df.loc[:,'intercept'] = 1.
            regression_confounders.append('intercept')
        logit = Logit(df[assignment], df[regression_confounders])
        model = logit.fit()
        if store_model_fit:
            self.propensity_score_model = model
        X.loc[:,'propensity score'] = model.predict(df[regression_confounders])
        return X

    def match(self, X, assignment='assignment', score='propensity score', n_neighbors=2, treated_value=1, control_value=0):
        """
        For each unit in the test group, match n_neighbors units in the control group with the closest propensity scores
        (matching with replacement).

        :param X: The data set, with (at least) an assignment, set of confounders, and an outcome
        :param assignment: A categorical variable (currently, 0 or 1) indicating test or control group, resp.
        :param score: The name of the column in X containing the propensity scores. Default is 'propensity score'
        :param n_neighbors: The number of neighbors to match to each unit.
        :return: two dataframes. the first contains the treatment units, and the second contains all of the control units
        that have been matched to the treatment units. The treatment unit dataframe (first dataframe) contains a new
        column with the indices of the matches in the control dataframe called 'matches'.
        """
        X = X.reset_index()
        treated = X[X[assignment] == treated_value]
        control = X[X[assignment] == control_value]
        neighbor_search = NearestNeighbors(metric='euclidean', n_neighbors=n_neighbors)
        neighbor_search.fit(control[[score]].values)
        treated.loc[:, 'matches'] = treated[score].apply(lambda x: self.get_matches(x, control, neighbor_search, score, n_neighbors))
        join_data = []
        for treatment_index, row in treated.iterrows():
            matches = row['matches'].flatten()
            for match in matches:
                join_data.append({'treatment_index': treatment_index, 'control_index': match})
        join_data = pd.DataFrame(join_data)
        matched_control = join_data.join(control, on='control_index')
        del treated['matches']
        del matched_control['control_index']
        return treated, matched_control


    def get_matches(self, score, control, knn, score_name, n_neighbors):
        """
        Discrete covariates can result in many unit having exactly the same propensity score. Since we don't get random
        neighbors, we'd end up using the same units over and over again when matching. Instead, we should find all units
        within the same distance as the closest n units, and randomly select matches from those.

        :param score: The score of the treatment unit we're matching
        :param control: the dataframe of control units we might match.
        :param knn: the K nearest neighbors model, a trained sklearn NearestNeighbors model
        :param score_name: The dataframe column in the control df with the propensity scores
        :param n_neighbors: The number of matches we'd like
        :return: The control indices of the matched units.
        """
        max_distance = max(knn.kneighbors(score)[0].flatten())
        lower_score = score - max_distance
        upper_score = score + max_distance
        gt = control[control[score_name] >= lower_score]
        return gt[gt[score_name] <= upper_score].sample(n_neighbors).index.values


    def estimate_treatments(self, treatments, matched_control, outcome):
        """
        Find the average outcome of the matched control units for each treatment unit. Add it to the treatment dataframe
        as a new column called 'control outcome'.

        :param treatments: A dataframe containing at least an outcome, and a list of indices for matches (in the control
        dataframe). This should be generated as the output of the self.match method.
        :param control: The dataframe containing the matches for the treatment dataframe. This should be generated as
        the output of the self.match method.
        :param outcome: A float or ordinal representing the outcome of interest.
        :return: The treatment dataframe with the matched control outcome for each unit in a new column,
        'control outcome'.
        """
        control_outcomes = matched_control.groupby('treatment_index').mean()[[outcome]]
        control_outcomes['control outcome'] = control_outcomes[outcome]
        del control_outcomes[outcome]
        return treatments.join(control_outcomes)

    def estimate_ATT(self, X, assignment, outcome, confounder_types, n_neighbors=5, bootstrap=False):
        """
        Estimate the average treatment effect for people who normally take the test assignment. Assumes a 1 for
        the test assignment, 0 for the control assignment.

        :param X: The data set, with (at least) an assignment, set of confounders, and an outcome
        :param assignment: A categorical variable (currently, 0 or 1) indicating test or control group, resp.
        :param outcome: The outcome of interest.  Should be real-valued or ordinal.
        :param confounder_types: A dictionary of variable_name: variable_type pairs of strings, where
        variable_type is in {'c', 'o', 'd'}, for 'continuous', 'ordinal', and 'discrete'.
        :param n_neighbors: An integer for the number of neighbors to use with k-nearest-neighbor matching
        :return: a float representing the treatment effect on the treated
        """
        X = self.score(X, confounder_types, assignment)
        treatments, matched_control = self.match(X, assignment=assignment, score='propensity score', n_neighbors=n_neighbors)
        df = treatments.append(matched_control)
        def estimate_ATT(df):
            treated = df[df[assignment]==1]
            control = df[df[assignment]==0]
            treatments = self.estimate_treatments(treated, control, outcome)
            y_hat_treated = treatments[outcome].mean()
            y_hat_control = treatments['control outcome'].mean()
            return y_hat_treated - y_hat_control
        if bootstrap:
            return bootstrap_statistic(df, estimate_ATT)
        else:
            return estimate_ATT(df)

    def estimate_ATC(self, X, assignment, outcome, confounder_types, n_neighbors=5, bootstrap=False):
        """
        Estimate the average treatment effect for people who normally take the control assignment. Assumes a 1 for
        the test assignment, 0 for the control assignment.

        :param X: The data set, with (at least) an assignment, set of confounders, and an outcome
        :param assignment: A categorical variable (currently, 0 or 1) indicating test or control group, resp.
        :param outcome: The outcome of interest.  Should be real-valued or ordinal.
        :param confounder_types: A dictionary of variable_name: variable_type pairs of strings, where
        variable_type is in {'c', 'o', 'd'}, for 'continuous', 'ordinal', and 'discrete'.
        :param n_neighbors: An integer for the number of neighbors to use with k-nearest-neighbor matching
        :return: a float representing the treatment effect on the control
        """
        df = X.copy()
        df[assignment] = (df[assignment] + 1) % 2
        if bootstrap:
            lower ,expec, upper = self.estimate_ATT(df, assignment, outcome, confounder_types,
                                                    n_neighbors=n_neighbors, bootstrap=bootstrap)
            return -lower, -expec, -upper

        else:
            return -self.estimate_ATT(df, assignment, outcome, confounder_types, n_neighbors=n_neighbors, bootstrap=bootstrap)

    def estimate_ATE(self, X, assignment, outcome, confounder_types, n_neighbors=5, bootstrap=False):
        """
        Find the Average Treatment Effect(ATE) on the population. An ATE can be estimated as a weighted average of the
        ATT and ATC, weighted by the proportion of the population who is treated or not, resp. Assumes a 1 for
        the test assignment, 0 for the control assignment.

        :param X: The data set, with (at least) an assignment, set of confounders, and an outcome
        :param assignment:  A categorical variable (currently, 0 or 1) indicating test or control group, resp.
        :param outcome: The outcome of interest.  Should be real-valued or ordinal.
        :param confounder_types: A dictionary of variable_name: variable_type pairs of strings, where
        variable_type is in {'c', 'o', 'd'}, for 'continuous', 'ordinal', and 'discrete'.
        :param n_neighbors: An integer for the number of neighbors to use with k-nearest-neighbor matching
        :return: a float representing the average treatment effect
        """
        att = self.estimate_ATT(X, assignment, outcome, confounder_types, n_neighbors=n_neighbors, bootstrap=bootstrap)
        atc = self.estimate_ATC(X, assignment, outcome, confounder_types, n_neighbors=n_neighbors, bootstrap=bootstrap)
        p_assignment = len(X[X[assignment] == 1]) / float(len(X))
        if bootstrap:
            lower = p_assignment*att[0] + (1-p_assignment)*atc[0]
            expec = p_assignment*att[1] + (1-p_assignment)*atc[1]
            upper = p_assignment*att[2] + (1-p_assignment)*atc[2]
            return lower, expec, upper
        else:
            return p_assignment*att + (1-p_assignment)*atc

    def assess_balance(self, X, assignment, confounder_types):
        """
        Given a data frame X, and a set of confounders, calculate the imbalance of the confounders over the (binary)
        treatment assignment. This makes a good optimization metric when choosing different regression models for
        the propensity score.

        :param X: The dataframe containing at least the assignment, the control variables, and the outcome variables.
        There's no need to turn the control variables into dummies -- that is handled automatically.
        :param assignment: The name of the column in the dataframe containing the binary treatment assignment.
        :param confounder_types: A dictionary containing the names of the columns in the dataframe holding the control
        variables, and the type of each of those variables ('c' = continuous, 'o' = ordinal, 'd' = discrete)
        :return: a dictionary containing the name of each control variable and the amount of imbalance on that variable.
        """
        imbalances = {}
        for confounder, confounder_type in confounder_types.items():
            if confounder_type != 'c':
                confounder_dummies = pd.get_dummies(X[confounder], prefix=confounder)
                X[confounder_dummies.columns] = confounder_dummies
                dummy_imbalances = []
                for dummy in confounder_dummies.columns:
                    dummy_imbalances.append(np.abs(self.calculate_imbalance(X, dummy, assignment)))
                imbalances[confounder] = sum(dummy_imbalances)
            else:
                imbalance = self.calculate_imbalance(X, confounder, assignment)
                imbalances[confounder] = imbalance
        return imbalances

    def calculate_imbalance(self, X, x, d):
        """
        Calculate the balance metric to assess how unbalanced x is across the two levels of (binary) treatment assignment,
        d.

        :param X: The data containing the test and control populations
        :param x: The name of the confounding column.
        :param d: The name of the treatment assignment variable.
        :return:
        """
        numerator = X[X[d] == 1].mean()[x] - X[X[d] == 0].mean()[x]
        denominator = np.sqrt((X[X[d] == 1].var()[x] + X[X[d] == 0].var()[x])/2.)
        return numerator / denominator