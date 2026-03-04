# Use an official Python runtime as a parent image (slim version to save space)
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# --no-cache-dir ensures Docker doesn't save the downloaded packages, saving disk space
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the working directory contents into the container
COPY . .

# Create the downloads directory inside the container
RUN mkdir -p downloads

# Run bot.py when the container launches
CMD ["python", "bot.py"]
