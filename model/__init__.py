"""model - Prediction model interface and implementations."""
from model.interface import PredictionModel
from model.example_model import ExampleHeuristicModel

__all__ = ["PredictionModel", "ExampleHeuristicModel"]
