# Databricks notebook source
# MAGIC %md #Time-Series analysis with Flint
# MAGIC https://github.com/twosigma/flint

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC There are a couple of issues around Flint on Databricks. Use the following custom JAR from here (instead of the one published on Maven): https://github.com/databricks/databricks-accelerators/tree/master/projects/databricks-flint

# COMMAND ----------

# MAGIC %md ## Initialization

# COMMAND ----------

# Enable Arrow-based columnar data transfers
spark.conf.set("spark.sql.execution.arrow.enabled", "true")
spark.conf.set("spark.sql.execution.arrow.fallback.enabled", "true")

# enable databricks io cache
spark.conf.set("spark.databricks.io.cache.enabled", "true")

# COMMAND ----------

# MAGIC %md ### 1. Time measurement

# COMMAND ----------

import datetime

stopwatch_start = datetime.datetime.now()
print("Started > " + str(stopwatch_start))

# COMMAND ----------

# MAGIC %md ### 2. Initialize flint and load the sensor data

# COMMAND ----------

from ts.flint import *

#create flint context
flintContext = FlintContext(sqlContext)

# COMMAND ----------

sensor_df = spark.sql("SELECT timestamp as time, nanoseconds, value, filename, sensorid FROM sensor_data")

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT sensorid, MIN(timestamp), MAX(timestamp), COUNT(*) as readings FROM sensor_data 
# MAGIC WHERE sensorid in ('sensor01', 'sensor02') 
# MAGIC GROUP BY sensorid

# COMMAND ----------

# MAGIC %md ## Parameters

# COMMAND ----------

# data schema parameters
timeColumn = 'time'
sensorNameColumn = 'sensorid'
sensorValueColumn = 'value'
sensorValueFilledColumn = sensorValueColumn + "_filled"

# COMMAND ----------

# sensor data alginment parameters
begin_time = "2018-12-12T09:00:00.000"
end_time = "2018-12-15T12:12:12.121"
sensors = ["sensor01", "sensor02"]
step_size = "2ms" # 2ms time slices 
join_tolerance = "2ms"  # time join tolerance of 2ms (past) 

# COMMAND ----------

# MAGIC %md ## Sensor data alignment

# COMMAND ----------

from ts.flint import windows

# create ts rdd using flint
def createTimeSeriesRDD(df, timeColumn, isSorted, timeUnit):
  #assumption the source RDD is sorted!
  return flintContext.read \
    .option('isSorted', isSorted) \
    .option('timeColumn', timeColumn) \
    .option('timeUnit', timeUnit) \
    .dataframe(df)

# COMMAND ----------

# MAGIC %md ### 1. Time slicing

# COMMAND ----------

#=============================================
# Option #1. using Pandas. Resource (memory intensive) - moves data to the driver
#=============================================
from datetime import date, datetime, timedelta

import pandas as pd
import numpy as np

def createTimeSlicedDataFramePandas(timeBegin, timeEnd, timeSteps):
  datetime_object_begin = datetime.strptime(timeBegin, '%Y-%m-%dT%H:%M:%S.%f')
  datetime_object_end = datetime.strptime(timeEnd, '%Y-%m-%dT%H:%M:%S.%f')
  
  # create the time slices
  # N: nanoseconds, U, us: microseconds, L, ms:	milliseconds, etc
  idx = pd.date_range(datetime_object_begin, datetime_object_end, freq=timeSteps)
  time = pd.DataFrame(idx, columns=[timeColumn])
  return spark.createDataFrame(time)

#=============================================
# Option #2. using Flint. scales better
#=============================================
def createTimeSlicedDataFrameFlint(timeBegin, timeEnd, timeSteps):
  return flintContext.read.range(timeBegin, timeEnd).clock('uniform', timeSteps)

# COMMAND ----------

def querySensorData(df, timeBegin, timeEnd, sensorNames):
  # create the filter expression and get the correct filter expression
  filterExpression = sensorNameColumn + " in (" + ', '.join('\'{}\''.format(sensor) for sensor in sensorNames) + ") and (" + timeColumn + " >= cast('" + timeBegin + "' as timestamp) and " + timeColumn + " <= cast('" + timeEnd + "' as timestamp))"
  return df.where(filterExpression).select(timeColumn,sensorNameColumn,sensorValueColumn).sort(timeColumn)

# COMMAND ----------

# create time slices
#Option #1. 
#timeSlices_df = createTimeSlicedDataFramePandas(begin_time, end_time, step_size)

#Option #2. 
timeSlices_df = createTimeSlicedDataFrameFlint(begin_time, end_time, step_size)

#cross join time and sensor df (and sort it by time)
sensor_filtered_df = spark.createDataFrame(pd.DataFrame(sensors, columns=[sensorNameColumn]))
base_df = timeSlices_df.crossJoin(sensor_filtered_df).sort(timeColumn)

# COMMAND ----------

# get relevant data from the base dataset
orig_filtered_df = querySensorData(sensor_df, begin_time, end_time, sensors)

# COMMAND ----------

# MAGIC %md ### 2. Algin sensor values (time-based join with tolerance) 

# COMMAND ----------

# join the dataframes on time using Flint
ts_base = createTimeSeriesRDD(base_df, timeColumn = timeColumn, isSorted = True, timeUnit = 'ms')
ts_df = createTimeSeriesRDD(orig_filtered_df, timeColumn = timeColumn, isSorted = True, timeUnit = 'ms')
tsJoined_df = ts_base.leftJoin(ts_df, tolerance = join_tolerance, key = [sensorNameColumn])

# COMMAND ----------

# MAGIC %md ### 3. Filling potential gaps

# COMMAND ----------

from pyspark.sql.types import *
from pyspark.sql import Window
from pyspark.sql.functions import last
from pyspark.sql.functions import pandas_udf, PandasUDFType

import sys

#=============================================
# OPTION #1: ForwardFill using Pandas UDF
#=============================================
# create a schema for the Pandas UDF
timeSeries_df_schema = StructType([ \
  StructField(timeColumn, TimestampType(), True), \
  StructField(sensorNameColumn, StringType(), True), \
  StructField(sensorValueColumn, DoubleType(), True)])

@pandas_udf(timeSeries_df_schema, PandasUDFType.GROUPED_MAP)
def ffill_pandas(pdf):
  pdf.loc[:, (sensorValueColumn)] = pdf.sort_values(by=[timeColumn])[[sensorValueColumn]].ffill()
  return pdf

#=============================================
# OPTION #2: ForwardFill using windows (taken from https://johnpaton.net/posts/forward-fill-spark/)
#=============================================
def ffill_windows(df, timeColumn, sensorColumn, toBeFilledColumn): 
  newfilledColumn = toBeFilledColumn + "_filled"
  
  # define the window
  window = Window.partitionBy(sensorColumn)\
                 .orderBy(timeColumn)\
                 .rowsBetween(-sys.maxsize, 0)

  # define the forward-filled column
  filled_column = last(df[toBeFilledColumn], ignorenulls=True).over(window)

  # do the fill
  df_filled = df.withColumn(newfilledColumn, filled_column)

  return df_filled

# COMMAND ----------

#forward fill potential missing values

# option #1 - fast but using 14 worker nodes errors indicate that we are running out of mem (used by PyArrow)
#tsJoined_filled_df = tsJoined_df.groupBy(sensorNameColumn).apply(ffill_pandas).sort(timeColumn)

# option #2 - slower but better resource utilization (no task failures)
tsJoined_filled_df = ffill_windows(tsJoined_df, timeColumn, sensorNameColumn, sensorValueColumn)

# COMMAND ----------

# MAGIC %md ## Results

# COMMAND ----------

tsJoined_filled_df.count()

# COMMAND ----------

# MAGIC %md ### 1. Time measurement

# COMMAND ----------

import datetime

stopwatch_end = datetime.datetime.now()
print("Finished > " + str(stopwatch_end))
print("Time elapsed > " + str(stopwatch_end-stopwatch_start))

# COMMAND ----------

# MAGIC %md ## Transform the long table to a wide table

# COMMAND ----------

# MAGIC %md ### 1. Create view and pivot the results

# COMMAND ----------

tsJoined_filled_df.createOrReplaceTempView("tempd")

# COMMAND ----------

sensorIds = ','.join(map("'{0}'".format, sensors))
sqlQuery = \
"SELECT * FROM ( " \
  "SELECT time, sensorid, value_filled " \
  "FROM tempd " \
") " \
"PIVOT (" \
  "min(value_filled) " \
  "FOR sensorid in (" + sensorIds + ") " \
") " \
"ORDER BY time "

pivotted_df = spark.sql(sqlQuery)

# COMMAND ----------

display(pivotted_df)

# COMMAND ----------

# MAGIC %md ### 2. Time measurement

# COMMAND ----------

import datetime

stopwatch_end = datetime.datetime.now()
print("Finished > " + str(stopwatch_end))
print("Time elapsed > " + str(stopwatch_end-stopwatch_start))