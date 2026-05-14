"""Parse solution.ipynb (or any notebook) and render a beginner-friendly
"Approach summary" panel describing what preprocessing, model(s), CV scheme,
and metric the current solution uses.

Usage (from supervisor.sh):
    python tools/summarize_approach.py <notebook_path> <out_html_path>

The script always writes a valid HTML fragment (<section id="approach"> ... </section>)
suitable for splicing into dashboard.html. If parsing fails, a short friendly
placeholder is emitted so the panel never disappears.

The emitted <section> includes a stable <div id="approachDisplay"> (between
<!--BEGIN_APPROACH_DISPLAY--> and <!--END_APPROACH_DISPLAY-->) for dashboard
per-metric snapshot extraction.

Output:
    - A "Preprocessing" column listing TF-IDF, scalers, embeddings, regex rules,
      tokenizers, memorization tables, anything detectable.
    - A "Model" column listing classifiers / regressors / ensembles / rule-based
      lookups / transformers / neural nets.
    - An "Evaluation" column listing CV strategy + metric + print-based self-eval.
    - A collapsible "How the gym works" beginner walkthrough.

Design goals:
    * Work for ANY MLE-bench / SWE-bench solution, not just scikit-learn.
    * Auto-detect arbitrary libraries by import path, not just a fixed allowlist.
    * Surface rule-based / regex / memorization pipelines so rule-heavy problems
      (e.g. text normalization) still produce a meaningful summary.
    * Do not infer a full approach from import lines alone. Import-only
      notebooks show a short "add training or evaluation code" placeholder.
    * Once real pipeline code exists, never leave a column empty: fall back to
      generic hints so the user still sees the notebook is wired up.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# -- Plain-English dictionary ------------------------------------------------
# Each entry: token -> (pretty label, plain-english one-liner)
# Tokens are matched as whole words (or as substrings if they contain ".").
EXPLAIN: Dict[str, Tuple[str, str]] = {
    # Text vectorizers ------------------------------------------------------
    "TfidfVectorizer": ("TF-IDF", "Turns text into numbers: common words count less, rare words count more."),
    "CountVectorizer": ("Bag-of-Words", "Turns text into numbers by counting how often each word appears."),
    "HashingVectorizer": ("Hashing trick", "Like bag-of-words but uses a hash, so it's fast and uses little memory."),
    # Scalers ---------------------------------------------------------------
    "StandardScaler": ("StandardScaler", "Shifts each feature to mean 0 and variance 1 so the model trains smoothly."),
    "MinMaxScaler": ("MinMaxScaler", "Rescales each feature into the range 0 to 1."),
    "RobustScaler": ("RobustScaler", "Scales using medians so outliers don't dominate."),
    "Normalizer": ("Normalizer", "Rescales each row to have unit length."),
    "PowerTransformer": ("PowerTransformer", "Warps each feature toward a bell curve so extreme values hurt less."),
    "QuantileTransformer": ("QuantileTransformer", "Reshapes each feature to a uniform or normal distribution."),
    # Encoders --------------------------------------------------------------
    "OneHotEncoder": ("One-hot encoding", "Turns each category into its own 0/1 column."),
    "OrdinalEncoder": ("Ordinal encoding", "Replaces each category with an integer code."),
    "LabelEncoder": ("Label encoding", "Turns class names (EAP / HPL / MWS) into integers the model can learn."),
    "TargetEncoder": ("Target encoding", "Replaces a category by the average target value seen in training."),
    # Decomposition / dim reduction -----------------------------------------
    "PCA": ("PCA", "Compresses many correlated features into a few directions that explain most of the variance."),
    "TruncatedSVD": ("Truncated SVD", "Like PCA but works on sparse matrices (great for TF-IDF)."),
    "NMF": ("NMF", "Finds additive, parts-based topics inside the data."),
    # Imputation ------------------------------------------------------------
    "SimpleImputer": ("SimpleImputer", "Fills missing values with mean/median/most-frequent."),
    "KNNImputer": ("KNN Imputer", "Fills missing values by looking at the K most similar rows."),
    "IterativeImputer": ("Iterative Imputer", "Fills missing values by regressing them on the other columns."),
    # Feature selection -----------------------------------------------------
    "SelectKBest": ("SelectKBest", "Keeps only the K features that look most predictive."),
    "VarianceThreshold": ("Variance filter", "Drops near-constant features that carry no signal."),
    "RFE": ("RFE", "Recursive Feature Elimination: drop the weakest feature, refit, repeat."),
    # Pipelines & composition -----------------------------------------------
    "Pipeline": ("Pipeline", "Chains preprocessing + model so they're applied in the same order every time."),
    "ColumnTransformer": ("ColumnTransformer", "Applies different preprocessing to different columns."),
    "FeatureUnion": ("FeatureUnion", "Computes several feature sets in parallel and stacks them."),
    "make_pipeline": ("make_pipeline", "Shortcut for building a Pipeline without naming each step."),
    # Linear models ---------------------------------------------------------
    "LogisticRegression": ("Logistic Regression", "Finds a weighted combination of features that predicts the class; smooth probabilities."),
    "SGDClassifier": ("SGD Classifier", "Trains linear models with stochastic gradient descent; scales to huge data."),
    "SGDRegressor": ("SGD Regressor", "Linear regression trained with stochastic gradient descent."),
    "Ridge": ("Ridge regression", "Linear regression that shrinks weights to reduce overfitting."),
    "RidgeClassifier": ("Ridge classifier", "Ridge regression adapted to classification."),
    "Lasso": ("Lasso regression", "Linear regression that pushes unhelpful weights to zero (feature selection)."),
    "ElasticNet": ("Elastic Net", "A mix of Ridge and Lasso regularization."),
    "LinearRegression": ("Linear regression", "Fits a straight line/plane that best predicts the target."),
    "BayesianRidge": ("Bayesian Ridge", "Ridge regression with Bayesian uncertainty estimates."),
    "HuberRegressor": ("Huber regressor", "Linear regression robust to outliers."),
    "PassiveAggressiveClassifier": ("Passive-Aggressive", "Online linear classifier that updates only on mistakes."),
    # Naive Bayes -----------------------------------------------------------
    "MultinomialNB": ("Multinomial NB", "Naive Bayes for word counts; simple, fast, strong baseline on text."),
    "ComplementNB": ("Complement NB", "NB variant that handles imbalanced text data better."),
    "BernoulliNB": ("Bernoulli NB", "Naive Bayes for binary features (word present/absent)."),
    "GaussianNB": ("Gaussian NB", "Naive Bayes assuming each feature is normally distributed."),
    # Trees / forests -------------------------------------------------------
    "DecisionTreeClassifier": ("Decision tree", "Splits the data with yes/no questions until each leaf predicts a class."),
    "DecisionTreeRegressor": ("Decision tree", "Regression tree that averages values in each leaf."),
    "RandomForestClassifier": ("Random forest", "Averages many decision trees trained on random subsets to reduce variance."),
    "RandomForestRegressor": ("Random forest", "Averages many decision trees for regression."),
    "ExtraTreesClassifier": ("Extra Trees", "Like random forest but uses random splits for extra diversity."),
    "ExtraTreesRegressor": ("Extra Trees", "Extra-random trees for regression."),
    "GradientBoostingClassifier": ("Gradient Boosting", "Builds trees one at a time, each one fixing the previous one's mistakes."),
    "GradientBoostingRegressor": ("Gradient Boosting", "Gradient-boosted trees for regression."),
    "HistGradientBoostingClassifier": ("HistGBT", "Fast gradient-boosted trees using histogram bins."),
    "HistGradientBoostingRegressor": ("HistGBT", "Fast gradient-boosted trees for regression."),
    "XGBClassifier": ("XGBoost", "Powerful gradient-boosted trees; often wins tabular competitions."),
    "XGBRegressor": ("XGBoost", "Powerful gradient-boosted trees for regression."),
    "LGBMClassifier": ("LightGBM", "Very fast gradient-boosted trees by Microsoft."),
    "LGBMRegressor": ("LightGBM", "Very fast gradient-boosted trees for regression."),
    "CatBoostClassifier": ("CatBoost", "Gradient boosting with built-in categorical handling by Yandex."),
    "CatBoostRegressor": ("CatBoost", "Yandex gradient boosting for regression."),
    # Neighbors / SVM / others ---------------------------------------------
    "KNeighborsClassifier": ("k-NN", "Predicts using the K nearest training examples."),
    "KNeighborsRegressor": ("k-NN regression", "Averages the K nearest training examples."),
    "SVC": ("SVM", "Finds the boundary that best separates classes with maximum margin."),
    "SVR": ("SVM regression", "Support-vector regression."),
    "LinearSVC": ("Linear SVM", "Linear SVM; fast on large sparse data like TF-IDF."),
    # Clustering ------------------------------------------------------------
    "KMeans": ("K-Means", "Groups rows into K clusters by distance to cluster centers."),
    "DBSCAN": ("DBSCAN", "Density-based clustering that finds irregular-shaped groups."),
    "AgglomerativeClustering": ("Hierarchical clustering", "Merges similar points bottom-up."),
    # Neural nets (top-level presence detection) ----------------------------
    "MLPClassifier": ("Simple neural net", "A small feed-forward neural network."),
    "MLPRegressor": ("MLP regressor", "A small feed-forward neural network for regression."),
    "nn.Module": ("PyTorch model", "A custom neural network written in PyTorch."),
    "torch.nn": ("PyTorch layers", "Neural-network building blocks (Linear, Conv, LSTM, ...)."),
    "torch.optim": ("PyTorch optimizer", "SGD/Adam/etc that updates the neural-network weights."),
    "keras": ("Keras model", "A neural network built with Keras / TensorFlow."),
    "tensorflow": ("TensorFlow", "Google's deep-learning framework."),
    "transformers": ("Transformer (HF)", "Uses a pretrained transformer model via Hugging Face."),
    "AutoModel": ("HF AutoModel", "Loads a pretrained transformer body."),
    "AutoTokenizer": ("HF AutoTokenizer", "Splits text into subword tokens for a transformer."),
    "AutoModelForSequenceClassification": ("HF Seq classifier", "Transformer with a classification head."),
    "Trainer": ("HF Trainer", "High-level training loop for Hugging Face models."),
    "pytorch_lightning": ("PyTorch Lightning", "Trainer/loop abstraction on top of PyTorch."),
    # Ensembling ------------------------------------------------------------
    "VotingClassifier": ("Voting ensemble", "Combines several models by averaging their votes."),
    "VotingRegressor": ("Voting ensemble", "Averages predictions of several regressors."),
    "StackingClassifier": ("Stacking", "Trains a meta-model on top of other models' predictions."),
    "StackingRegressor": ("Stacking", "Stacks regressors with a meta-regressor."),
    "BaggingClassifier": ("Bagging", "Trains many copies of a model on bootstrap samples and averages them."),
    # CV strategies ---------------------------------------------------------
    "StratifiedKFold": ("Stratified K-Fold", "Splits data into folds while keeping the class balance in each fold."),
    "KFold": ("K-Fold", "Splits data into K equal folds for cross-validation."),
    "GroupKFold": ("Group K-Fold", "K-Fold that keeps all rows of the same group together."),
    "TimeSeriesSplit": ("Time-series split", "Trains on the past, validates on the future (no leakage)."),
    "train_test_split": ("Train/test split", "A single random split into training and validation sets."),
    "cross_val_score": ("cross_val_score", "Runs K-Fold CV and returns one score per fold."),
    "cross_validate": ("cross_validate", "K-Fold CV returning multiple metrics per fold."),
    "GridSearchCV": ("GridSearchCV", "Tries every combination of hyperparameters with CV."),
    "RandomizedSearchCV": ("RandomizedSearchCV", "Samples random hyperparameter combos with CV."),
    "optuna": ("Optuna", "Hyperparameter search using Bayesian / TPE sampling."),
    # Metrics ---------------------------------------------------------------
    "log_loss": ("log loss", "Penalizes confident-but-wrong predictions; lower is better."),
    "accuracy_score": ("accuracy", "Fraction of predictions that are exactly right."),
    "balanced_accuracy_score": ("balanced accuracy", "Accuracy averaged over classes; robust to imbalance."),
    "roc_auc_score": ("ROC-AUC", "How well predicted scores rank positives above negatives."),
    "f1_score": ("F1 score", "Balance between precision and recall; higher is better."),
    "precision_score": ("precision", "Of the items we flagged positive, how many really are."),
    "recall_score": ("recall", "Of the truly positive items, how many we caught."),
    "confusion_matrix": ("confusion matrix", "Table of predicted vs true classes."),
    "mean_squared_error": ("MSE", "Average squared error; penalizes big mistakes heavily."),
    "root_mean_squared_error": ("RMSE", "Root of MSE; same units as the target."),
    "mean_absolute_error": ("MAE", "Average absolute error; robust to outliers."),
    "r2_score": ("R\u00b2", "Fraction of variance the model explains (1 = perfect)."),
    "classification_report": ("classification report", "Per-class precision/recall/F1 table."),
    # Text / NLP preprocessing ---------------------------------------------
    "num2words": ("num2words", "Converts numbers to their spoken form (e.g. 12 \u2192 \"twelve\")."),
    "word_tokenize": ("NLTK word tokenizer", "Splits a sentence into words using NLTK."),
    "sent_tokenize": ("NLTK sentence tokenizer", "Splits text into sentences using NLTK."),
    "WordNetLemmatizer": ("WordNet lemmatizer", "Reduces words to a dictionary base form."),
    "PorterStemmer": ("Porter stemmer", "Chops word endings (running \u2192 run) with Porter's rules."),
    "SnowballStemmer": ("Snowball stemmer", "Multi-language stemmer."),
    "stopwords": ("stopwords list", "Common words (\"the\", \"a\") removed before feature extraction."),
    "spacy": ("spaCy", "Industrial NLP pipeline: tokenization, POS, NER, lemmas."),
    "Tokenizer": ("Tokenizer", "Splits text into subword tokens for a model."),
    "sentencepiece": ("SentencePiece", "Subword tokenizer used by many transformer models."),
    "inflect": ("inflect.engine", "English inflection (pluralization, number-to-words)."),
    # Image preprocessing ---------------------------------------------------
    "torchvision": ("torchvision transforms", "Standard image augmentations (resize, crop, flip, normalize)."),
    "Compose": ("Transform Compose", "Chains several image/text transforms."),
    "Resize": ("Resize", "Resizes images to a target size."),
    "RandomHorizontalFlip": ("Random flip", "Flips images left\u2194right for augmentation."),
    "Normalize": ("Tensor Normalize", "Subtracts mean/divides by std per channel."),
    "albumentations": ("albumentations", "Fast image augmentation library."),
    "cv2": ("OpenCV", "Image I/O and classic CV operations."),
    "PIL": ("Pillow (PIL)", "Image loading and manipulation."),
    "Image.open": ("Pillow open", "Loads an image from disk as a PIL object."),
    # Tabular helpers -------------------------------------------------------
    "pandas": ("pandas", "Tabular data: CSV I/O, filtering, grouping, joining."),
    "numpy": ("numpy", "Vectorized numeric arrays and linear algebra."),
    "scipy": ("scipy", "Scientific computing: sparse matrices, stats, signal, etc."),
    "polars": ("polars", "Lightning-fast columnar DataFrame library."),
    "dask": ("dask", "Parallel/out-of-core dataframes and arrays."),
    # Misc ML helpers -------------------------------------------------------
    "joblib": ("joblib", "Parallel execution and model persistence on disk."),
    "pickle": ("pickle", "Serializes any Python object to bytes."),
    "tqdm": ("tqdm", "Progress bars for long loops."),
}

PREPROC_KEYS = {
    "TfidfVectorizer", "CountVectorizer", "HashingVectorizer",
    "StandardScaler", "MinMaxScaler", "RobustScaler", "Normalizer",
    "PowerTransformer", "QuantileTransformer",
    "OneHotEncoder", "OrdinalEncoder", "LabelEncoder", "TargetEncoder",
    "PCA", "TruncatedSVD", "NMF",
    "SimpleImputer", "KNNImputer", "IterativeImputer",
    "SelectKBest", "VarianceThreshold", "RFE",
    "Pipeline", "ColumnTransformer", "FeatureUnion", "make_pipeline",
    # text / NLP
    "num2words", "word_tokenize", "sent_tokenize", "WordNetLemmatizer",
    "PorterStemmer", "SnowballStemmer", "stopwords", "spacy",
    "Tokenizer", "sentencepiece", "inflect",
    # image
    "torchvision", "Compose", "Resize", "RandomHorizontalFlip",
    "albumentations", "cv2", "PIL", "Image.open",
    # tabular utilities commonly used as preprocessing surface
    "polars", "dask",
}

MODEL_KEYS = {
    "LogisticRegression", "SGDClassifier", "SGDRegressor",
    "Ridge", "RidgeClassifier", "Lasso", "ElasticNet", "LinearRegression",
    "BayesianRidge", "HuberRegressor", "PassiveAggressiveClassifier",
    "MultinomialNB", "ComplementNB", "BernoulliNB", "GaussianNB",
    "DecisionTreeClassifier", "DecisionTreeRegressor",
    "RandomForestClassifier", "RandomForestRegressor",
    "ExtraTreesClassifier", "ExtraTreesRegressor",
    "GradientBoostingClassifier", "GradientBoostingRegressor",
    "HistGradientBoostingClassifier", "HistGradientBoostingRegressor",
    "XGBClassifier", "XGBRegressor", "LGBMClassifier", "LGBMRegressor",
    "CatBoostClassifier", "CatBoostRegressor",
    "KNeighborsClassifier", "KNeighborsRegressor", "SVC", "SVR", "LinearSVC",
    "KMeans", "DBSCAN", "AgglomerativeClustering",
    "MLPClassifier", "MLPRegressor",
    "VotingClassifier", "VotingRegressor",
    "StackingClassifier", "StackingRegressor", "BaggingClassifier",
    "nn.Module", "torch.nn", "torch.optim", "keras", "tensorflow",
    "transformers", "AutoModel", "AutoTokenizer",
    "AutoModelForSequenceClassification", "Trainer", "pytorch_lightning",
}

CV_KEYS = {
    "StratifiedKFold", "KFold", "GroupKFold", "TimeSeriesSplit",
    "train_test_split", "cross_val_score", "cross_validate",
    "GridSearchCV", "RandomizedSearchCV", "optuna",
}

METRIC_KEYS = {
    "log_loss", "accuracy_score", "balanced_accuracy_score", "roc_auc_score",
    "f1_score", "precision_score", "recall_score", "confusion_matrix",
    "mean_squared_error", "root_mean_squared_error", "mean_absolute_error",
    "r2_score", "classification_report",
}


# -- Pattern-based technique detection --------------------------------------
# Each pattern: (regex, bucket, pretty label, plain-english description).
# Buckets: "preproc", "model", "cv", "metric".
# These catch rule-based / memorization / regex / deep-learning patterns even
# when no class name is used (common in custom NLP / text-normalization work).
PATTERNS: List[Tuple[str, str, str, str]] = [
    # ---------------- preprocessing patterns ------------------------------
    (r"\bre\.(?:compile|match|search|sub|findall|fullmatch)\b|\bre\.IGNORECASE\b",
     "preproc", "Regex rules",
     "Uses Python's re module to match/replace patterns in text."),
    (r"\bpd\.read_csv\s*\([^)]*chunksize\s*=",
     "preproc", "Chunked CSV streaming",
     "Streams a large CSV in chunks so it fits in memory."),
    (r"\bCounter\s*\(",
     "preproc", "collections.Counter",
     "Counts occurrences to compute frequencies or a mode-lookup table."),
    (r"\bdefaultdict\s*\(",
     "preproc", "collections.defaultdict",
     "Dictionary with an auto-created default value, handy for aggregation."),
    (r"\.str\.(?:lower|upper|strip|replace|contains|split)\b|\.lower\(\)|\.upper\(\)|\.strip\(\)",
     "preproc", "String normalization",
     "Lowercases / strips / replaces raw strings before matching or modeling."),
    (r"\bstopwords\.words\b|STOPWORDS",
     "preproc", "Stopword filtering",
     "Drops common filler words before feature extraction."),
    (r"\bnum2words\s*\(",
     "preproc", "num2words",
     "Converts numbers to their spoken form for text normalization."),
    (r"\bword_tokenize\s*\(|\bsent_tokenize\s*\(",
     "preproc", "NLTK tokenizer",
     "Splits text into words/sentences with NLTK."),
    (r"\bspacy\.load\s*\(",
     "preproc", "spaCy pipeline",
     "Loads a spaCy NLP pipeline (tokenizer, tagger, NER...)."),
    (r"\bAutoTokenizer\.from_pretrained\s*\(",
     "preproc", "HF tokenizer",
     "Loads a pretrained transformer tokenizer."),
    (r"\bsentencepiece\b|\bSentencePieceProcessor\b",
     "preproc", "SentencePiece",
     "Subword tokenizer often used by transformer models."),
    (r"\bPIL\.Image\.open\b|\bImage\.open\b",
     "preproc", "Pillow image load",
     "Loads image files with Pillow."),
    (r"\bcv2\.imread\b",
     "preproc", "OpenCV imread",
     "Loads image files with OpenCV."),
    (r"\btorchvision\.transforms\b",
     "preproc", "torchvision transforms",
     "Standard image augmentation and normalization pipeline."),
    (r"\balbumentations\b",
     "preproc", "albumentations",
     "Fast image augmentation library."),
    (r"\bdf\[[^]]+\]\.fillna\s*\(|\.fillna\s*\(",
     "preproc", "Missing-value fill",
     "Imputes missing cells with a constant / mean / forward-fill."),
    (r"\bpd\.get_dummies\s*\(",
     "preproc", "get_dummies",
     "One-hot encodes a categorical column with pandas."),
    # ---------------- model / approach patterns ---------------------------
    (r"\.most_common\s*\(",
     "model", "Mode lookup (memorization)",
     "For each input, pick the most common target seen in training (a dictionary baseline)."),
    (r"\blookup\s*=\s*\{|\bmapping\s*=\s*\{|\btable\s*=\s*\{",
     "model", "Dictionary lookup",
     "Maps inputs to outputs via a hard-coded/learned dictionary."),
    (r"def\s+fallback\s*\(|def\s+\w*normalize\w*\s*\(|def\s+\w*rule\w*\s*\(",
     "model", "Rule-based function",
     "Hand-written rules applied when the lookup table has no answer."),
    (r"\bmodel\.fit\s*\(|\.fit_predict\s*\(",
     "model", "Model fit",
     "Trains a model using a .fit() call."),
    (r"\.predict_proba\s*\(",
     "model", "Probabilistic prediction",
     "Outputs class probabilities via .predict_proba()."),
    (r"\bnn\.Sequential\s*\(|\bnn\.Linear\s*\(|\bnn\.Conv2d\s*\(|\bnn\.LSTM\s*\(",
     "model", "PyTorch network",
     "Custom neural network using torch.nn layers."),
    (r"\btf\.keras\.Sequential\b|\bkeras\.layers\b",
     "model", "Keras network",
     "Neural network built with Keras layers."),
    (r"\btransformers\.pipeline\s*\(",
     "model", "HF pipeline",
     "Hugging Face high-level inference pipeline."),
    (r"\bAutoModel\w*\.from_pretrained\s*\(",
     "model", "Pretrained transformer",
     "Loads a pretrained transformer model from Hugging Face."),
    # ---------------- evaluation patterns ---------------------------------
    (r"VAL_ACC\s*:|validation_accuracy\s*:",
     "metric", "Self-eval print (VAL_ACC)",
     "Prints a validation score that the supervisor reads for the trend chart."),
    (r"\bcorrect\s*/\s*total\b|\bacc\s*=\s*correct\s*/",
     "metric", "Manual accuracy",
     "Computes correct/total on a held-out slice to estimate accuracy."),
    (r"\bnp\.mean\s*\(\s*y_?pred\s*==\s*y_?true\s*\)",
     "metric", "np.mean accuracy",
     "Accuracy = fraction of predictions equal to the targets."),
    (r"\bhold.?out\b|\btrain_test_split\b",
     "cv", "Hold-out split",
     "Single split into train and validation sets."),
    (r"\bfor\s+\w+\s+in\s+kf\.split\b|\bKFold\b",
     "cv", "K-Fold loop",
     "Cross-validation loop across K folds."),
    (r"\bearly_stopping_rounds\b|\bEarlyStopping\b",
     "cv", "Early stopping",
     "Stops training when validation score stops improving."),
]


# -- Import-path bucketing --------------------------------------------------
# Maps a module prefix to a bucket so arbitrary imports still get classified.
MODULE_BUCKET: List[Tuple[str, str]] = [
    # sklearn submodules
    ("sklearn.preprocessing", "preproc"),
    ("sklearn.feature_extraction", "preproc"),
    ("sklearn.feature_selection", "preproc"),
    ("sklearn.decomposition", "preproc"),
    ("sklearn.impute", "preproc"),
    ("sklearn.pipeline", "preproc"),
    ("sklearn.compose", "preproc"),
    ("sklearn.model_selection", "cv"),
    ("sklearn.metrics", "metric"),
    ("sklearn.ensemble", "model"),
    ("sklearn.linear_model", "model"),
    ("sklearn.tree", "model"),
    ("sklearn.naive_bayes", "model"),
    ("sklearn.neighbors", "model"),
    ("sklearn.svm", "model"),
    ("sklearn.cluster", "model"),
    ("sklearn.neural_network", "model"),
    ("sklearn.dummy", "model"),
    # boosting frameworks
    ("xgboost", "model"),
    ("lightgbm", "model"),
    ("catboost", "model"),
    # deep-learning frameworks
    ("torch.nn", "model"),
    ("torch.optim", "model"),
    ("torchvision.transforms", "preproc"),
    ("torchvision", "model"),
    ("torch", "model"),
    ("tensorflow.keras", "model"),
    ("tensorflow", "model"),
    ("keras", "model"),
    ("transformers", "model"),
    ("pytorch_lightning", "model"),
    ("sentence_transformers", "model"),
    ("timm", "model"),
    ("fastai", "model"),
    # NLP preprocessing
    ("nltk", "preproc"),
    ("spacy", "preproc"),
    ("num2words", "preproc"),
    ("inflect", "preproc"),
    ("sentencepiece", "preproc"),
    ("tokenizers", "preproc"),
    ("ftfy", "preproc"),
    ("unicodedata", "preproc"),
    ("unidecode", "preproc"),
    # image preprocessing
    ("albumentations", "preproc"),
    ("cv2", "preproc"),
    ("PIL", "preproc"),
    ("skimage", "preproc"),
    # eval / tuning
    ("optuna", "cv"),
    ("hyperopt", "cv"),
    ("ray.tune", "cv"),
    # general (keep last so more specific prefixes win)
    ("pandas", "preproc"),
    ("polars", "preproc"),
    ("dask", "preproc"),
    ("scipy.sparse", "preproc"),
    ("scipy.stats", "metric"),
    ("scipy", "preproc"),
    ("numpy", "preproc"),
    ("re", "preproc"),
    ("regex", "preproc"),
    ("collections", "preproc"),
    ("itertools", "preproc"),
    ("joblib", "model"),
]


def _load_notebook(nb_path: Path) -> Tuple[str, int, int]:
    """Return (concatenated source, code_cell_count, markdown_cell_count)."""
    try:
        data = json.loads(nb_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return "", 0, 0
    pieces: List[str] = []
    code_n = md_n = 0
    for cell in data.get("cells", []):
        src = cell.get("source", "")
        if isinstance(src, list):
            src = "".join(src)
        pieces.append(src or "")
        ct = cell.get("cell_type")
        if ct == "code":
            code_n += 1
        elif ct == "markdown":
            md_n += 1
    return "\n".join(pieces), code_n, md_n


_IMPORT_LINE_ONLY = re.compile(
    r"^\s*(?:from\s+[\w.]+\s+import\s+|import\s+)",
)


def _code_without_import_lines(src: str) -> str:
    """Remove import/from lines so we do not treat bare imports as a pipeline."""
    return "\n".join(ln for ln in src.splitlines() if not _IMPORT_LINE_ONLY.match(ln))


def _has_executable_pipeline(body: str) -> bool:
    """True when the notebook has real training, inference, CV, or scoring code."""
    b = body.strip()
    if not b:
        return False
    return bool(
        re.search(r"\.(?:fit|fit_transform|partial_fit)\s*\(", b)
        or re.search(r"\.predict(?:_proba)?\s*\(", b)
        or re.search(r"\bVAL_ACC\s*:", b)
        or re.search(r"\bvalidation_accuracy\s*:", b)
        or re.search(r"\bcross_val_score\s*\(|\bcross_validate\s*\(", b)
        or re.search(
            r"\b(?:log_loss|roc_auc_score|accuracy_score|mean_squared_error|"
            r"mean_absolute_error|r2_score|f1_score)\s*\(",
            b,
        )
    )


def _detect_ngram_ranges(src: str) -> Dict[str, List[str]]:
    ranges: Dict[str, List[str]] = {"TfidfVectorizer": [], "CountVectorizer": []}
    for cls in ranges:
        for m in re.finditer(rf"{cls}\s*\((.*?)\)", src, re.DOTALL):
            args = m.group(1)
            analyzer = re.search(r"analyzer\s*=\s*['\"]([^'\"]+)['\"]", args)
            ngram = re.search(r"ngram_range\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", args)
            label = []
            if analyzer:
                a = analyzer.group(1)
                label.append({"word": "word", "char": "char", "char_wb": "char"}.get(a, a))
            if ngram:
                lo, hi = ngram.group(1), ngram.group(2)
                label.append(f"{lo}-{hi} grams" if lo != hi else f"{lo}-grams")
            ranges[cls].append(" ".join(label) if label else "")
    return ranges


_BOOST_CLS = re.compile(
    r"\b(LGBMClassifier|LGBMRegressor|XGBClassifier|XGBRegressor|"
    r"CatBoostClassifier|CatBoostRegressor|HistGradientBoostingClassifier|HistGradientBoostingRegressor|"
    r"GradientBoostingClassifier|GradientBoostingRegressor)\s*\(",
)

_BOOST_PRETTY = {
    "LGBMClassifier": "LightGBM classifier",
    "LGBMRegressor": "LightGBM regressor",
    "XGBClassifier": "XGBoost classifier",
    "XGBRegressor": "XGBoost regressor",
    "CatBoostClassifier": "CatBoost classifier",
    "CatBoostRegressor": "CatBoost regressor",
    "HistGradientBoostingClassifier": "HistGradientBoosting classifier",
    "HistGradientBoostingRegressor": "HistGradientBoosting regressor",
    "GradientBoostingClassifier": "sklearn GradientBoosting classifier",
    "GradientBoostingRegressor": "sklearn GradientBoosting regressor",
}


def _balanced_paren_content(src: str, open_idx: int) -> Optional[str]:
    """Return inner text for the '(' at *open_idx*, or None if unbalanced."""
    depth = 0
    for j in range(open_idx, len(src)):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1 : j]
    return None


def _detect_boosting_param_lines(src: str) -> List[str]:
    """Extra list items for Model column: constructor kwargs for tree/boosting libraries."""
    items: List[str] = []
    param_patterns = (
        (r"num_leaves\s*=\s*(\d+)", "num_leaves"),
        (r"learning_rate\s*=\s*([\d.eE+-]+)", "learning_rate"),
        (r"n_estimators\s*=\s*(\d+)", "n_estimators"),
        (r"max_depth\s*=\s*([\d-]+)", "max_depth"),
        (r"subsample\s*=\s*([\d.]+)", "subsample"),
        (r"colsample_bytree\s*=\s*([\d.]+)", "colsample_bytree"),
        (r"reg_alpha\s*=\s*([\d.]+)", "reg_alpha"),
        (r"reg_lambda\s*=\s*([\d.]+)", "reg_lambda"),
        (r"min_child_samples\s*=\s*(\d+)", "min_child_samples"),
        (r"max_bin\s*=\s*(\d+)", "max_bin"),
        (r"iterations\s*=\s*(\d+)", "iterations"),
        (r"\bdepth\s*=\s*(\d+)", "depth"),
        (r"l2_leaf_reg\s*=\s*([\d.]+)", "l2_leaf_reg"),
        (r"objective\s*=\s*['\"]([^'\"]+)['\"]", "objective"),
        (r"early_stopping_rounds\s*=\s*(\d+)", "early_stopping_rounds"),
    )
    for m in _BOOST_CLS.finditer(src):
        open_idx = m.end() - 1
        inner = _balanced_paren_content(src, open_idx)
        if not inner:
            continue
        cls = m.group(1)
        found: List[str] = []
        for pat, key in param_patterns:
            mm = re.search(pat, inner, re.I)
            if mm:
                found.append(f"{key}={mm.group(1)}")
            if len(found) >= 10:
                break
        if not found:
            snippet = re.sub(r"\s+", " ", inner).strip()
            if len(snippet) > 140:
                snippet = snippet[:137] + "..."
            if snippet:
                found.append(f"constructor args (snippet): {snippet}")
        label = _BOOST_PRETTY.get(cls, cls)
        detail = ", ".join(found[:10])
        items.append(
            _render_item(
                f"{label} parameters",
                "Hyperparameters read from the constructor call in your notebook.",
                detail,
            )
        )
    return items


def _find_tokens(src: str, vocab) -> List[str]:
    found: List[str] = []
    for name in vocab:
        if "." in name:
            if name in src:
                found.append(name)
        else:
            if re.search(rf"\b{re.escape(name)}\b", src):
                found.append(name)
    order = {n: m.start() for n in found if (m := re.search(rf"(?:\b|^){re.escape(n)}", src))}
    found.sort(key=lambda n: order.get(n, 10**9))
    return found


# -- Import scanning --------------------------------------------------------
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w\.]+)\s+import\s+([^\n#]+)|import\s+([^\n#]+))",
    re.MULTILINE,
)


def _scan_imports(src: str) -> List[Tuple[str, str]]:
    """Return list of (module, name_as_used_in_code).

    For "from X import A, B as C" -> [("X", "A"), ("X", "C")].
    For "import X.Y as Z"        -> [("X.Y", "Z")].
    For "import X, Y"            -> [("X", "X"), ("Y", "Y")].
    """
    out: List[Tuple[str, str]] = []
    for m in _IMPORT_RE.finditer(src):
        frm, names, imp = m.group(1), m.group(2), m.group(3)
        if frm:
            for part in names.split(","):
                p = part.strip().split(" as ")
                base = p[0].strip()
                alias = (p[1].strip() if len(p) > 1 else base)
                if base and base != "*":
                    out.append((frm, alias))
        elif imp:
            for part in imp.split(","):
                p = part.strip().split(" as ")
                mod = p[0].strip()
                alias = (p[1].strip() if len(p) > 1 else mod)
                if mod:
                    out.append((mod, alias))
    return out


def _bucket_for_module(mod: str) -> str | None:
    """Return bucket name for a given module path using MODULE_BUCKET prefixes."""
    for prefix, bucket in MODULE_BUCKET:
        if mod == prefix or mod.startswith(prefix + "."):
            return bucket
    return None


# -- Rendering helpers ------------------------------------------------------
def _h(text: str) -> str:
    return html.escape(text, quote=False)


def _render_item(label: str, desc: str, detail: str | None = None) -> str:
    suffix = f" <span class=\"approach-dim\">({_h(detail)})</span>" if detail else ""
    return f'<li><b>{_h(label)}</b>{suffix} \u2014 {_h(desc)}</li>'


def _fallback_item(bucket: str, src: str, code_n: int) -> str:
    """Generic fallback so a bucket is never empty if the notebook has code."""
    if code_n <= 0:
        return '<li class="empty">Add code cells to <code>solution.ipynb</code> to populate this panel.</li>'
    hints = {
        "preproc": (
            "Custom preprocessing",
            f"No standard library detected; {code_n} code cell(s) handle data loading/cleaning.",
        ),
        "model": (
            "Custom logic",
            "No standard ML model detected \u2014 this solution appears to be rule-based or lookup-driven.",
        ),
        "cv": (
            "No explicit CV",
            "No cross-validation helper detected; evaluation may be a simple hold-out or self-check.",
        ),
        "metric": (
            "No explicit metric",
            "No standard metric helper detected; check notebook outputs for ad-hoc scoring.",
        ),
    }
    label, desc = hints.get(bucket, ("\u2014", "Nothing to show here."))
    return _render_item(label, desc)


def _pending_pipeline_html(notebook_path: Path, code_n: int, md_n: int) -> str:
    """Placeholder when the notebook is mostly imports or has no train/eval code yet."""
    nb_meta = f"{code_n} code cell(s), {md_n} markdown cell(s)"
    return f"""<section id="approach" class="panel approach">
  <div class="approach-header">
    <h2>Approach summary</h2>
    <span id="approachSelectionLabel" class="approach-sub">Showing latest metric run.</span>
    <span class="approach-sub">Summaries are built from <b>executable</b> code in <code>{_h(notebook_path.name)}</code> &mdash; {_h(nb_meta)}. Import-only cells are ignored until you train or evaluate.</span>
  </div>
  <div id="trajectorySummary" class="trajectory-panel"></div>
  <div id="approachDisplay">
<!--BEGIN_APPROACH_DISPLAY-->
  <div class="empty approach-pending">
    <p><b>No approach details yet.</b> This panel lists preprocessing, models, and evaluation only after your notebook actually <em>does</em> something&mdash;for example <code>.fit(...)</code>, <code>.fit_transform(...)</code>, <code>.predict_proba(...)</code>, <code>train_test_split(...)</code>, cross-validation, common metric calls, or a <code>VAL_ACC:</code> line.</p>
    <p class="approach-dim">Bare <code>import</code> lines alone are not shown here so the summary stays faithful to your implementation.</p>
  </div>
  <details class="approach-howto">
    <summary>How the gym works (beginner walkthrough)</summary>
    <ol>
      <li><b>Pull</b> a problem with <code>aicodinggym mle download &lt;id&gt;</code>. The dataset lands in <code>data/</code> and <code>description.md</code> explains the task.</li>
      <li><b>Build</b> <code>solution.ipynb</code>: load the data, preprocess it (turn raw text/tables into numbers or apply rules), fit a model or look things up, then write <code>submission.csv</code>.</li>
      <li><b>Print</b> a line like <code>VAL_ACC: 0.91</code> at the end of the notebook. Higher is better. The supervisor reads that number and plots it.</li>
      <li><b>Save</b>. The supervisor auto-runs the notebook, logs a card here, and refreshes this summary so you can see what your pipeline looks like.</li>
      <li><b>Submit</b> when you're happy: <code>aicodinggym mle submit &lt;id&gt; -F submission.csv</code>.</li>
    </ol>
  </details>
<!--END_APPROACH_DISPLAY-->
  </div>
</section>"""


def build_html(notebook_path: Path) -> str:
    src, code_n, md_n = _load_notebook(notebook_path)
    if not src.strip():
        return _empty_section(f"No notebook content found at <code>{_h(str(notebook_path))}</code>.")

    body = _code_without_import_lines(src)
    if not _has_executable_pipeline(body):
        return _pending_pipeline_html(notebook_path, code_n, md_n)

    # --- curated token scan (executable code only; import lines removed) ---
    ngrams = _detect_ngram_ranges(body)
    extra: Dict[str, str] = {}
    for cls, labels in ngrams.items():
        clean = [l for l in labels if l]
        if clean:
            extra[cls] = ", ".join(sorted(set(clean)))

    preproc_tok = _find_tokens(body, PREPROC_KEYS)
    model_tok = _find_tokens(body, MODEL_KEYS)
    cv_tok = _find_tokens(body, CV_KEYS)
    metric_tok = _find_tokens(body, METRIC_KEYS)

    # --- pattern scan (buckets: preproc/model/cv/metric) ---
    pattern_hits: Dict[str, List[Tuple[str, str]]] = {"preproc": [], "model": [], "cv": [], "metric": []}
    for pat, bucket, label, desc in PATTERNS:
        if re.search(pat, body):
            if (label, desc) not in pattern_hits[bucket]:
                pattern_hits[bucket].append((label, desc))

    # --- import scan (non-import lines only: avoids "TF-IDF" from a bare import) ---
    # Bare stdlib imports that are plumbing, not a "technique" worth showing
    # (their actual usage is surfaced by the PATTERNS block more specifically).
    STDLIB_SKIP = {
        "re", "regex", "collections", "itertools", "functools", "os", "sys",
        "json", "csv", "math", "time", "datetime", "pathlib", "typing",
        "hashlib", "random", "string", "io", "copy", "warnings", "logging",
        "argparse", "pickle", "subprocess", "glob", "shutil", "zipfile",
    }
    imports = _scan_imports(body)
    seen_mods: set[str] = set()
    import_hits: Dict[str, List[Tuple[str, str]]] = {"preproc": [], "model": [], "cv": [], "metric": []}
    for mod, alias in imports:
        if mod in seen_mods:
            continue
        seen_mods.add(mod)
        top = mod.split(".")[0]
        if top in STDLIB_SKIP:
            continue
        bucket = _bucket_for_module(mod)
        if not bucket:
            continue
        pretty_key = top
        pretty = EXPLAIN.get(pretty_key, (pretty_key, None))[0]
        desc = EXPLAIN.get(pretty_key, (None, None))[1] or f"Library used in the current pipeline (module {mod})."
        if (pretty, desc) not in import_hits[bucket]:
            import_hits[bucket].append((pretty, desc))

    # --- assemble HTML lists per bucket (token-curated first, then patterns,
    #     then imports). De-duplicate on label text so we don't repeat. ---

    def merge_bucket(
        token_keys: List[str],
        bucket: str,
        token_extra: Dict[str, str] | None = None,
    ) -> str:
        items: List[str] = []
        seen_labels: set[str] = set()
        token_extra = token_extra or {}
        # curated tokens first
        for k in token_keys:
            pretty, desc = EXPLAIN.get(k, (k, "Used in the current solution."))
            if pretty in seen_labels:
                continue
            seen_labels.add(pretty)
            items.append(_render_item(pretty, desc, detail=token_extra.get(k)))
        # pattern hits
        for label, desc in pattern_hits[bucket]:
            if label in seen_labels:
                continue
            seen_labels.add(label)
            items.append(_render_item(label, desc))
        # import hits
        for label, desc in import_hits[bucket]:
            if label in seen_labels:
                continue
            seen_labels.add(label)
            items.append(_render_item(label, desc))
        if not items:
            return _fallback_item(bucket, body, code_n)
        return "\n".join(items)

    preproc_html = merge_bucket(preproc_tok, "preproc", token_extra=extra)
    model_html = merge_bucket(model_tok, "model")
    for boost_line in _detect_boosting_param_lines(body):
        model_html += "\n" + boost_line
    cv_html = merge_bucket(cv_tok, "cv")
    metric_html = merge_bucket(metric_tok, "metric")

    blend = _blend_hint(body)
    if blend:
        model_html += "\n" + _render_item("Blend", blend)

    nb_meta = f"{code_n} code cell(s), {md_n} markdown cell(s)"
    return f"""<section id="approach" class="panel approach">
  <div class="approach-header">
    <h2>Approach summary</h2>
    <span id="approachSelectionLabel" class="approach-sub">Showing latest metric run.</span>
    <span class="approach-sub">Inferred from executable code in <code>{_h(notebook_path.name)}</code> \u2014 {_h(nb_meta)}. Bare import lines are ignored; refreshes on save.</span>
  </div>
  <div id="trajectorySummary" class="trajectory-panel"></div>
  <div id="approachDisplay">
<!--BEGIN_APPROACH_DISPLAY-->
  <div class="approach-grid">
    <div class="approach-col">
      <h3>Preprocessing</h3>
      <ul>{preproc_html}</ul>
    </div>
    <div class="approach-col">
      <h3>Model</h3>
      <ul>{model_html}</ul>
    </div>
    <div class="approach-col">
      <h3>Evaluation</h3>
      <ul>
        {cv_html}
        {metric_html}
      </ul>
    </div>
  </div>
  <details class="approach-howto">
    <summary>How the gym works (beginner walkthrough)</summary>
    <ol>
      <li><b>Pull</b> a problem with <code>aicodinggym mle download &lt;id&gt;</code>. The dataset lands in <code>data/</code> and <code>description.md</code> explains the task.</li>
      <li><b>Build</b> <code>solution.ipynb</code>: load the data, preprocess it (turn raw text/tables into numbers or apply rules), fit a model or look things up, then write <code>submission.csv</code>.</li>
      <li><b>Print</b> a line like <code>VAL_ACC: 0.91</code> at the end of the notebook. Higher is better. The supervisor reads that number and plots it.</li>
      <li><b>Save</b>. The supervisor auto-runs the notebook, logs a card here, and refreshes this summary so you can see what your pipeline looks like.</li>
      <li><b>Submit</b> when you're happy: <code>aicodinggym mle submit &lt;id&gt; -F submission.csv</code>.</li>
    </ol>
  </details>
<!--END_APPROACH_DISPLAY-->
  </div>
</section>"""


def _blend_hint(src: str) -> str | None:
    if re.search(r"best\s*=?\s*w\s*\*\s*(oof_|pred_)", src) or re.search(r"blend\s*=\s*w\s*\*", src):
        return "Linear blend of model probabilities, weight tuned on validation."
    return None


def _empty_section(message: str) -> str:
    return f"""<section id="approach" class="panel approach">
  <div class="approach-header">
    <h2>Approach summary</h2>
    <span id="approachSelectionLabel" class="approach-sub">Showing latest metric run.</span>
  </div>
  <div id="trajectorySummary" class="trajectory-panel"></div>
  <div id="approachDisplay">
<!--BEGIN_APPROACH_DISPLAY-->
  <div class="empty">{message}</div>
<!--END_APPROACH_DISPLAY-->
  </div>
</section>"""


# Maps agent-written role names to the 3-column grid (0=Preprocessing, 1=Model, 2=Evaluation)
_ROLE_COLUMN: Dict[str, int] = {
    "setup": 0,
    "preprocessing": 0,
    "feature-engineering": 1,
    "model": 1,
    "evaluation": 2,
    "submission": 2,
}
_COLUMN_HEADERS = ["Preprocessing", "Model", "Evaluation"]


def build_html_from_agent_cells(cells: list, nb_path: Path) -> str:
    """Render 3-column approach grid from agent-written notebook_analysis.cells."""
    cols: List[List[str]] = [[], [], []]
    for cell in cells:
        role = str(cell.get("role", "model")).lower().strip()
        col_idx = _ROLE_COLUMN.get(role, 1)
        summary = _h(str(cell.get("summary", "")))
        why = _h(str(cell.get("why", "")))
        label = _h(role.replace("-", " ").title())
        item = f"<li><b>{label}</b> — {summary}"
        if why:
            item += f' <span style="color:var(--muted);font-size:11.5px;">({why})</span>'
        item += "</li>"
        cols[col_idx].append(item)
    col_htmls = []
    for header, items in zip(_COLUMN_HEADERS, cols):
        inner = "<ul>" + "".join(items) + "</ul>" if items else '<ul><li class="empty">No cells assigned to this stage.</li></ul>'
        col_htmls.append(f'<div class="approach-col"><h3>{header}</h3>{inner}</div>')
    grid = '<div class="approach-grid">' + "".join(col_htmls) + "</div>"
    sub = _h(nb_path.name)
    return f"""<section id="approach" class="panel approach">
  <div class="approach-header">
    <h2>Approach summary</h2>
    <span id="approachSelectionLabel" class="approach-sub">Showing latest metric run.</span>
    <span class="approach-sub">Written by agent from <code>{sub}</code> — cell-by-cell breakdown.</span>
  </div>
  <div id="trajectorySummary" class="trajectory-panel"></div>
  <div id="approachDisplay">
<!--BEGIN_APPROACH_DISPLAY-->
  {grid}
<!--END_APPROACH_DISPLAY-->
  </div>
</section>"""


def main(argv: List[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("notebook_path")
    parser.add_argument("out_html_path")
    parser.add_argument("--agent-cells", default="", dest="agent_cells")
    args, _ = parser.parse_known_args(argv[1:])
    nb = Path(args.notebook_path)
    out = Path(args.out_html_path)
    try:
        if args.agent_cells:
            import json as _json
            cells = _json.loads(args.agent_cells)
            if isinstance(cells, list) and cells:
                html_frag = build_html_from_agent_cells(cells, nb)
            else:
                html_frag = build_html(nb) if nb.exists() else _empty_section(
                    f"Create <code>{_h(nb.name)}</code> to see the approach summary here."
                )
        else:
            html_frag = build_html(nb) if nb.exists() else _empty_section(
                f"Create <code>{_h(nb.name)}</code> to see the approach summary here."
            )
    except Exception as exc:
        html_frag = _empty_section(f"Could not parse notebook: {_h(str(exc))}.")
    out.write_text(html_frag, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
