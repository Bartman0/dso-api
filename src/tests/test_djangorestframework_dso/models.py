from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        app_label = 'test_djangorestframework_dso'


class Movie(models.Model):
    name = models.CharField(max_length=100)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True)

    class Meta:
        app_label = 'test_djangorestframework_dso'


