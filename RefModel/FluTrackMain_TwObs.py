"""
Main script to run the hierarchical spatio-temporal model for CP7

@author: Ssu-Hsin Yu (syu@ssci.com)
"""
from sys import argv

import simplejson as json
from collections import OrderedDict
import numpy as np
import scipy.sparse as spa
from datetime import datetime
import scipy.special

import SaveSmpl


import GIS_Utilities
#from GaussianMRF import GaussianMRF
import FluSpread_TwObs as FluSpread

# To read observations and construct measurement matrix
import ConstructMmentMatrix as CM
# To generate covariates based on vaccination & tweet data in JSON
import DefineCovariates as DC

if len(argv) < 3:
    print(argv)
    raise SystemExit("Usage: %s input_dir output_dir" % argv[0])

root = argv[1] + '/'

ProbSet = None
for size in 'Full', 'Middle', 'Small':
    if size in root:
        ProbSet = size
if ProbSet is None:
    raise SystemExit("unable to parse data subset name")

# CDC ILI rates
data_fn = root + 'Flu_ILI.csv'
# ILI observation precisions
obserr_fn = root + 'Flu_ILI_ErrCoef.csv'
# file that stores adjacent counties of each county in US (lower 48)
adj_fn = root + 'county_adjacency_lower48.txt'
# files that stores covariates in JSON format
VaccTweets_fn = (root + "Flu_Vacc_Tweet_TRAIN.json",
                 root + "Flu_Vacc_Tweet_TEST.json")
# file that stores the mapping from regions/states/districs to counties
map_fn = root + 'Region2CountyMap.json'
# file that stores weekly tweet counts in each states
dataTweet_fn = root + 'FilterStateTweetCounts.csv'
obserrTweet_fn = None # None if a fixed tweet error distribution is selected a priori


#_________________________________________________________________________
## JSON file that stores the results
#
#

result_fn = argv[2] + "/result.json"


# specify whether to use sparse matrix formulation
isSparse = True

#_________________________________________________________________________
## Specify the earlies and latest dates for processing
#
# If the dates are empty strings, then their values are defined by
#  the earlies or latest dates of the covariates
earliest=datetime.strptime('09/28/2013','%m/%d/%Y')
latest=datetime.strptime('05/23/2015','%m/%d/%Y')
#latest=datetime.strptime('10/28/2013','%m/%d/%Y')
#latest=datetime.strptime('04/19/2014','%m/%d/%Y')


#_________________________________________________________________________
## Specify the states of interest for processing in 2-letter state postal code
# If empty or None, then all states are of interest.

if ProbSet is 'Small':
    STATES = ['MS']
elif ProbSet is 'Middle':
    STATES = ['AL', 'FL', 'GA', 'KY', 'MS', 'NC', 'SC', 'TN']
    #STATES = ['MS', 'TN']
elif ProbSet is 'Full':
    STATES = []
else:
    raise SystemExit('No such problem set!')



## Load all of the data and prepare them for processing
#

# construct county adjacency matrix
(AdjMatrix, CountyName, CountyAdjDict)=GIS_Utilities.ConstructCountyAdjMatrix(adj_fn,STATES=STATES)

# construct spatial structural matrix from the adjacency matrix
Dw = np.squeeze(np.asarray(AdjMatrix.sum(axis=1)))
StrucMat = spa.spdiags(Dw,0,AdjMatrix.shape[0],AdjMatrix.shape[0]) - AdjMatrix


# DEBUG -- to ensure precision matrix is non-singular to test simulation
#StrucMat[0,0]=StrucMat[0,0] + 1.
StrucMat[0,0]=StrucMat[0,0]

if not isSparse:
    StrucMat = StrucMat.todense()

# read the covariates
with open(VaccTweets_fn[0], 'rb') as fobj:
    # the first file
    CntData = json.load(fobj, object_pairs_hook=OrderedDict)
for fn in VaccTweets_fn[1:]:
    # subsequent files
    with open(fn, 'rb') as fobj:
        tmpCntData = json.load(fobj, object_pairs_hook=OrderedDict)
    for key in tmpCntData.keys():
        if key in CntData:
            for wk in tmpCntData[key]['No. of Tweets']:
                CntData[key]['No. of Tweets'][wk] = tmpCntData[key]['No. of Tweets'][wk]
            if 'Vaccination percentage %' in tmpCntData[key]:
                # no vaccination data available on the state level and 3 counties
                for wk in tmpCntData[key]['Vaccination percentage %']:
                    CntData[key]['Vaccination percentage %'][wk] = tmpCntData[key]['Vaccination percentage %'][wk]
            #else:
                #print 'Vacc:', key, ' count:', count
    

# determine the earliest and latest dates if they haven't been
# specified earlier
if (not earliest) or (not latest): # if either of them is empty
    # find ealiest and latest dates
    fips = "01001" # use Autauga, AL as an example
    dates = []
    dates.extend([datetime.strptime(d, '%m/%d/%Y')
                  for d in CntData[fips]['Vaccination percentage %'].keys()])
    if not earliest: # no earliest date specified
        earliest = min(dates)
    if not latest: # no latest date specified
        latest = max(dates)
# total number of weeks
num_weeks = int((latest - earliest).days / 7) + 1

# number of cells in temporal dimension
temporalGrid = num_weeks

# number of cells in spatial dimension
spatialGrid = len(CountyName)


class Parameters(object):
    ''' An empty class object to store anything similar to the structure format
    '''
    pass

#_________________________________________________________________________
# Define random effects terms for GLMM
#
# Each element in the list defines a random-effect term.
RandEff_Param = list()


# The following definition of Random Effects is an example of spatio-temporal
# model where the spatial relationship is explicitly defined by a structural
# matrix and the temporal relationship is defined by a basis.
RandEff_Param.append(Parameters())
RandEff_Param[0].Method = "GaussianMRF" # random effects method GMRF
RandEff_Param[0].StrucMat = ("StrucM", "Basis") # define the methods to form the structural matrix for each dimension
RandEff_Param[0].StrucMatParam = (StrucMat, np.array([-1,1])) # For each dimension, defines the basis                      
RandEff_Param[0].RankDef = (1, 0) # the total rank deficiency of each element of StrucMat
RandEff_Param[0].RandomFieldWeight = np.array([1.0, 20.0]) # For each dimension, defines the weighting
RandEff_Param[0].N = (spatialGrid, temporalGrid) # For each dimension, the number of random variables (grid points)
#RandEff_Param[0].Dim = (0, 1) # The corresponding grid dimension

# The overall precision to be mutiplied to the normalized structure matrix
#RandEff_Param[0].RandomField_Prec = 5.
RandEff_Param[0].RandomField_Prec = 1.

# Define hyperparameters for the multiplicative coefficient of the precision matrix
# The mean value of variance is GammaMean time RandomField_Prec
GammaMean = 2.0 #5.0 # mean of Gamma distribution
GammaVar = 2.0 # variance of Gamma distribution
beta = GammaMean / GammaVar
alpha = GammaMean * beta
# (PDF, {arguments for PDF})
# If PDF is an empty string (lenght 0), then no hyperparameters are specified
# and the precision is explicitly determined by RandomField_Prec
RandEff_Param[0].HyperParam = ("Gamma", {"alpha": alpha, "beta": beta})
#RandEff_Param[0].HyperParam = ("", {})



#_________________________________________________________________________
# Define fixed effects for GLMM
#

# Define Covariates of Fixed Effects
#-----------------------------------
#
# If there is no fixed effects, simply set
# FixedEff_Param.FixedEffects = ""
# then anything else for FixedEff_Param will be ignored

# Covariates for the fixed effects. The covariates are defined for each node.

# information for each county to be used as covariates
#CovName = ('No. of Tweets', 'Vaccination percentage %', 'Population per square mile, 2010')
#CovName = ('No. of Tweets (state total)',)
CovName = ('Population per square mile, 2010',)
# sepcify the covariates that need to be filtered
#smooth = {'No. of Tweets (state total)': ("median", {"kernel_size":3})}
smooth = {}
# read covariates related to county vaccination and tweets
Covariates = DC.DefineCovariates(CntData, CountyName, earliest, latest, CovName,
                                 map_fn=map_fn, smooth=smooth)


# Define Coefficients of Fixed Effects
#-------------------------------------
#

FixedEff_Param = Parameters()

# "Estimated": to be estimate from data
# If it is empty, then no fixed effects are present
FixedEff_Param.FixedEffects = "Estimated"
#FixedEff_Param.FixedEffects = ""

# Prior distributions of the fixed-effects coefficients to be estimated
# The priors have Gaussian distributions with each element corresponding to a
# fixed-effect coefficient term.

# (No. Tweets, Vacc %, Pop density)
#FixedEff_Param.Coeff_PriorMean = np.array([0., 0.])
#FixedEff_Param.Coeff_PriorPrec = np.array([0.2, 0.2])

# (Pop density)
FixedEff_Param.Coeff_PriorMean = np.array([0.])
FixedEff_Param.Coeff_PriorPrec = np.array([0.2])

# Covariates
FixedEff_Param.Covariates = Covariates


# Define coefficients related to each set of observations
#-------------------------------------
#
# The first set is always None; the other sets of the coefficients need to be
# defined if there are more than one set of observations.

ObsCoef_Param = list()

ObsCoef_Param.append(None) # The first set is always None

# if there are more than 1 set of observation
ObsCoef_Param.append(Parameters())
ObsCoef_Param[1].PriorMean = np.array([10, 10]) #(slope, intercept)
ObsCoef_Param[1].PriorPrec = np.array([10, 10])


#_________________________________________________________________________
# Define link function and observation distribution for GLMM
#

# specify the region (as defined in the header of csv truth file) not to be
# considered. This is typically used to avoid conflicting/overlapping regions.
#Region2Avoid = ['HHS Region 4', 'NC'] 
Region2Avoid = [] 

# read observed data and C matrix (as in Cx)
if obserr_fn is None:
    (C_obs, ObsDataILI) = CM.ConstructMmentMatrix(data_fn, map_fn, CountyName,
                                           (earliest, latest), CountyInfo=CntData,
                                            region2avoid=Region2Avoid)
else:
    (C_obs, ObsDataILI, ErrDataFlat) = CM.ConstructMmentMatrix(data_fn, map_fn, CountyName,
                                           (earliest, latest), obserr_fn=obserr_fn,
                                            CountyInfo=CntData, region2avoid=Region2Avoid)
                                           
ObsData = list()
ObsData.append(ObsDataILI)

'''
# set the observation to be identity matrix to save data of all counties
# observation C matrix (as in Cx)
num_row = Covariates.shape[0]
if isSparse:
    C_obs = spa.eye(num_row)
else:
    C_obs = np.eye(num_row)
'''

# Beta distribution coefficients
if obserr_fn is None:
    #beta_prec = 5000 # the larger the value, the smaller the variance
    #beta_prec = 1000 # the larger the value, the smaller the variance
    beta_prec = 40000. # the larger the value, the smaller the variance
else:
    beta_prec = ErrDataFlat

LinkObs_Param = list()
LinkObs_Param.append(Parameters())
LinkObs_Param[0].LinkFunc = "linear"
#LinkObs_Param[0].ObsDist = "Beta"
LinkObs_Param[0].ObsDist = "TruncatedNormal"
LinkObs_Param[0].LinkCoef= [C_obs, beta_prec]


'''
Second set of observations -- weekly state tweet counts
'''

# read observed data and C matrix (as in Cx)
if obserrTweet_fn is None:
    (C_obsTw, ObsDataTw) = CM.ConstructMmentMatrix(dataTweet_fn, map_fn, CountyName,
                                           (earliest, latest), CountyInfo=CntData,
                                            region2avoid=Region2Avoid)
else:
    (C_obsTw, ObsDataTw, ErrDataFlatTw) = CM.ConstructMmentMatrix(dataTweet_fn, map_fn, CountyName,
                                           (earliest, latest), obserr_fn=obserrTweet_fn,
                                            CountyInfo=CntData, region2avoid=Region2Avoid)

NrmlCnst = 100000. # nomalization constant (per NrmlCnst people)
ObsData.append(ObsDataTw*NrmlCnst)

# Beta distribution coefficients
if obserrTweet_fn is None:
    beta_precTw = 2. # the larger the value, the smaller the variance
else:
    beta_precTw = ErrDataFlatTw

LinkObs_Param.append(Parameters())
LinkObs_Param[1].LinkFunc = "linear"
#LinkObs_Param[1].ObsDist = "Beta"
LinkObs_Param[1].ObsDist = "TruncatedNormal"
LinkObs_Param[1].LinkCoef= [C_obsTw, beta_precTw]


#_________________________________________________________________________
# Define parameters related to estimation of LGCP coefficients

UpdateParams = Parameters()
UpdateParams.Method = 'MCMC' # method for PyMC estimation (either 'MCMC' or 'MAP')
#UpdateParams.Method = 'MAP'
UpdateParams.NumSample =  5000000 # total number of epochs (including burn-in) in MCMC 
UpdateParams.NumBurnIn =  4999999 # number of epochs during burn-in
UpdateParams.Thinning = 1 # thining the number of epochs to be stored

# The database file where the previous MCMC runs are stored.
# None: start from scratch, and store the results in 'samples.pkl'
# 'xxxx.pkl': a pickle file that stores previous results from which to continue MCMC sampling
# 'donot store': don't store any results in a file

# UpdateParams.SessionDatabaseFile = 'flu_est.pkl'
UpdateParams.SessionDatabaseFile = None
#UpdateParams.SessionDatabaseFile = 'donot store'


#_________________________________________________________________________
## Initialization (creating a generative model object)

gridParams = Parameters()
gridParams.N = Covariates.shape[0] # total number of nodes

print 'Initialization'
a = FluSpread.GLMM(RandEff_Param, FixedEff_Param, LinkObs_Param,
                   gridParams, isSparse = isSparse, isNoSample = True,
                   ObsCoef_Param = ObsCoef_Param)

#_________________________________________________________________________
## Update (MCMC sampling of posterior)

print 'Update'
a.Update(ObsData, UpdateParams)

#print 'Draw samples'
#Smpl = a.DrawSamples()

#_________________________________________________________________________
## Draw Samples from the posterior and save the average results
SAVERESULT = 1
if SAVERESULT:

    result_fn = "test.json"
    start_date = earliest
    end_date = latest
    # multiplied by 100 to be percentage
    #SaveSmpl.SaveSmpl(Smpl*100, result_fn, CountyName, start_date, end_date)
    #SaveSmpl.SaveSmpl(Covariates, 'test2.json', CountyName, start_date, end_date)
    #SaveSmpl.SaveSmpl(100.*np.mean(a.Result['Predictor'][:,:,0],axis=0), result_fn, CountyName, start_date, end_date)
    SaveSmpl.SaveSmpl(100.*scipy.special.expit(np.mean(a.Result['Predictor'][:,:,0],axis=0)), result_fn, CountyName, start_date, end_date)

 