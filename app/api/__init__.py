from fastapi import APIRouter
from app.api import auth, classes, packages, trainers

api_router = APIRouter()

# Include routers
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(classes.router, prefix="/classes", tags=["classes"])
api_router.include_router(packages.router, prefix="/packages", tags=["packages"])
api_router.include_router(trainers.router, prefix="/trainers", tags=["trainers"])
