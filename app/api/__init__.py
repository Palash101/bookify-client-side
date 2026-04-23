from fastapi import APIRouter
from app.api import auth, classes, class_bookings, packages, trainers, locations, fitness_programs, gym, wallet
from app.payments import routes as payment_routes

api_router = APIRouter()

# Include routers
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(class_bookings.router, prefix="/classes", tags=["bookings"])
api_router.include_router(classes.router, prefix="", tags=["classes"])
api_router.include_router(packages.router, prefix="/packages", tags=["packages"])
api_router.include_router(trainers.router, prefix="/trainers", tags=["trainers"])
api_router.include_router(locations.router, prefix="/locations", tags=["locations"])
api_router.include_router(
    fitness_programs.router,
    prefix="",
    tags=["training-programs"],
)
api_router.include_router(gym.router, prefix="/gym", tags=["gym"])
api_router.include_router(wallet.router, prefix="")
api_router.include_router(
    payment_routes.router,
    prefix="",
)
